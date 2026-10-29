"""End-to-end AI pipeline: JD in, output folder out (layout fixing comes later).

    folder = run(jd_text)                                  # company/role as Claude read them
    folder = run(jd_text, company="Google", role="SRE")    # explicit names win

Writes into output/<Company>_<Role>_<YYYY-MM-DD>[_N]/:
    CV_<CANDIDATE_FILENAME>.pdf / .docx   rendered by cvgen.render.render() (LibreOffice)
    cv.json                               exactly the dict that was rendered
    cv.meta.json                          source ids, gaps, repairs, token usage, model
    job_description.txt                   the JD exactly as given
If validation still fails after the repair attempts, the folder gets cv.draft.json and
cv.draft.meta.json instead of a CV, and run() raises PipelineError naming it.
"""

import json
import logging
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from cvgen.output import candidate_filename, create_output_dir
from cvgen.render import render
from cvgen.tailor import TailorError, TailorValidationError, tailor

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = REPO_ROOT / "profile.json"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output"

log = logging.getLogger(__name__)


class PipelineError(TailorError):
    pass


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jd(folder: Path, jd: str) -> None:
    with open(folder / "job_description.txt", "w", encoding="utf-8", newline="") as f:
        f.write(jd)


def run(jd: str, company: str | None = None, role: str | None = None, *, profile_path: Path = DEFAULT_PROFILE,
        output_root: Path = DEFAULT_OUTPUT_ROOT, client=None, today: date | None = None) -> Path:
    load_dotenv(REPO_ROOT / ".env")
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))

    try:
        result = tailor(profile, jd, client=client)
    except TailorValidationError as exc:
        failed = exc.result
        folder = create_output_dir(output_root, company or failed.company, role or failed.role, today)
        _write_jd(folder, jd)
        _write_json(folder / "cv.draft.json", failed.cv)
        _write_json(folder / "cv.draft.meta.json", failed.meta())
        raise PipelineError(f"{exc}\nDraft saved in {folder} (cv.draft.json); fix it by hand and render it.") from exc

    final_company, final_role = company or result.company, role or result.role
    folder = create_output_dir(output_root, final_company, final_role, today)
    log.info("writing %s", folder)
    _write_jd(folder, jd)

    meta = result.meta()
    meta.update(company=final_company, role=final_role)
    if (company and company != result.company) or (role and role != result.role):
        meta["extracted"] = {"company": result.company, "role": result.role}
    _write_json(folder / "cv.meta.json", meta)

    _write_json(folder / "cv.json", result.cv)
    docx_path, pdf_path = render(result.cv, folder, f"CV_{candidate_filename()}")
    if not Path(pdf_path).is_file():  # render() doesn't check LibreOffice's exit code
        raise PipelineError(f"PDF was not created: {pdf_path}. Is LibreOffice installed and not already running headless?")
    log.info("done: %s", folder)
    return folder
