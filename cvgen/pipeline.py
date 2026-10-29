"""End-to-end AI pipeline: JD in, output folder out.

    folder = run(jd_text)                                  # company/role as Claude read them
    folder = run(jd_text, company="Google", role="SRE")    # explicit names win

Writes into output/<Company>_<Role>_<YYYY-MM-DD>[_N]/:
    CV_<CANDIDATE_FILENAME>.pdf / .docx   rendered by cvgen.render.render() (LibreOffice), after
                                          cvgen.layout.fix_layout() fixed widows / page overflow
    cv.json                               exactly the dict that was rendered
    cv.meta.json                          source ids, gaps, repairs, layout rounds, token usage, model
    job_description.txt                   the JD exactly as given
If validation still fails after the repair attempts, the folder gets cv.draft.json and
cv.draft.meta.json instead of a CV, and run() raises PipelineError naming it.
"""

import json
import logging
import os
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from cvgen import limits
from pydantic import ValidationError

from cvgen import costs
from cvgen.gaps import gap_report, update_backlog, write_gap_report
from cvgen.layout import LayoutReport, check_layout, fix_layout
from cvgen.output import candidate_filename, create_output_dir
from cvgen.profile import check_profile
from cvgen.render import anonymize, get_soffice_path, render
from cvgen.tailor import TailorError, TailorValidationError, tailor
from cvgen.validate import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = REPO_ROOT / "profile.json"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output"
MAX_RETRIES = 6
COSTS_CSV = "costs.csv"  # one summary line per CV, in the output root
ANON_BASENAME = "CV_JohnDoe"  # same name and anonymize() as the legacy generate.py

log = logging.getLogger(__name__)


class PipelineError(TailorError):
    pass


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jd(folder: Path, jd: str) -> None:
    with open(folder / "job_description.txt", "w", encoding="utf-8", newline="") as f:
        f.write(jd)


class SetupError(PipelineError):
    """Something to fix before running: API key, LibreOffice, or profile.json."""


# ---------------------------
# SETUP CHECKS
# ---------------------------
def check_api_key() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SetupError(
            "ANTHROPIC_API_KEY is not set. Put it in .env in the repo root (copy .env.example) "
            "or set it in your environment.")


def check_libreoffice() -> str:
    try:
        return get_soffice_path()
    except RuntimeError:
        raise SetupError(
            "LibreOffice (soffice) was not found. Install it from https://www.libreoffice.org/download/ "
            "and put its program folder on PATH, or install it to C:\\Program Files\\LibreOffice.") from None


def load_profile_checked(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise SetupError(f"Profile not found: {path}. Create it from profile.example.json.")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SetupError(f"{path} is not valid JSON: {exc}") from None
    errors = check_profile(profile).errors
    if errors:
        shown = "\n".join(f"  - {e}" for e in errors[:10]) + (f"\n  ... and {len(errors) - 10} more" if len(errors) > 10 else "")
        raise SetupError(f"{path} has {len(errors)} error(s):\n{shown}\nRun `cvgen profile check {path}` for the full list.")
    return profile


def render_anonymized(cv: dict, folder: Path) -> Path | None:
    """Render CV_JohnDoe.docx/.pdf next to the real CV, exactly as generate.py does: name, contact
    details, links, company and university names replaced. Returns the PDF, or None if LibreOffice
    didn't produce one (logged; the real CV is already done)."""
    _, pdf = render(anonymize(cv), folder, ANON_BASENAME)
    if not Path(pdf).is_file():
        log.warning("anonymized PDF was not created: %s", pdf)
        return None
    return Path(pdf)


def make_client() -> anthropic.Anthropic:
    # The SDK retries rate limits (429), overloaded (529), 5xx and connection errors with
    # exponential backoff, honouring retry-after; give it more attempts than the default 2.
    return anthropic.Anthropic(max_retries=MAX_RETRIES)


# ---------------------------
# RUN
# ---------------------------
def run(jd: str, company: str | None = None, role: str | None = None, *, pages: int | None = None,
        layout_fix: bool = True, gaps: bool = True, model: str | None = None, progress=None,
        profile_path: Path = DEFAULT_PROFILE, output_root: Path = DEFAULT_OUTPUT_ROOT, client=None,
        today: date | None = None) -> Path:
    """Tailor, validate, render, fix the layout and write the gap report; return the output folder.

    `pages` overrides limits.MAX_PAGES for this run; `layout_fix=False` renders and checks
    the layout without asking Claude to fix it; `gaps=False` skips the skill-gap report;
    `progress` gets a message per stage. A failed gap report is recorded, not fatal.
    """
    progress = progress or (lambda message: None)
    load_dotenv(REPO_ROOT / ".env")
    if not jd.strip():
        raise SetupError("The job description is empty.")
    profile = load_profile_checked(profile_path)
    if client is None:
        check_api_key()
        check_libreoffice()
        client = make_client()

    with limits.override(MAX_PAGES=pages) if pages else nullcontext():
        try:
            result = tailor(profile, jd, client=client, model=model, progress=progress)
        except TailorValidationError as exc:
            failed = exc.result
            folder = create_output_dir(output_root, company or failed.company, role or failed.role, today)
            _write_jd(folder, jd)
            _write_json(folder / "cv.draft.json", failed.cv)
            draft_meta = failed.meta()
            draft_meta["calls"] = failed.usage.get("calls", [])
            draft_meta["cost_usd"] = costs.totals(draft_meta["calls"])["cost_usd"]
            _write_json(folder / "cv.draft.meta.json", draft_meta)
            costs.append_csv(Path(output_root) / COSTS_CSV, folder.name, failed.model, draft_meta["calls"])
            raise PipelineError(f"{exc}\nDraft saved in {folder}. Fix cv.draft.json by hand, save it as cv.json, "
                                f"then run: cvgen rerender \"{folder}\"") from exc

        final_company, final_role = company or result.company, role or result.role
        folder = create_output_dir(output_root, final_company, final_role, today)
        log.info("writing %s", folder)
        _write_jd(folder, jd)

        # Render, check widows and page count, and let Claude fix them; the kept version is left rendered.
        try:
            cv, sources, layout_meta = fix_layout(
                result.cv, result.sources, profile, jd, folder, f"CV_{candidate_filename()}", client=client,
                max_rounds=None if layout_fix else 0, progress=progress)  # LAYOUT_MODEL
        except TailorError as exc:
            raise PipelineError(f"{exc}. Is LibreOffice installed and not already running headless?") from exc
        pages_limit = limits.MAX_PAGES

    progress("Rendering the anonymized CV")
    render_anonymized(cv, folder)

    gap_meta, gap_calls = None, []
    if gaps:
        try:
            gap = gap_report(profile, jd, client=client, progress=progress)  # GAP_MODEL, not the tailor model
            write_gap_report(folder, gap, final_company, final_role)
            update_backlog(output_root)
            gap_meta = {"fit": gap.report["fit"]["score"], "repairs": len(gap.repairs), "web_search": gap.web_search,
                        "usage": gap.usage}
            gap_calls = gap.usage.get("calls", [])
        except (TailorError, anthropic.APIError, ValidationError) as exc:  # the CV is done; keep it
            log.warning("gap report failed: %s", exc)
            progress(f"Gap report failed: {str(exc).splitlines()[0]}")
            gap_meta = {"error": str(exc)}
            gap_calls = getattr(exc, "calls", [])

    meta = result.meta()
    meta.update(company=final_company, role=final_role, sources=sources, layout=layout_meta,
                settings={"pages": pages_limit, "layout_fix": layout_fix, "gaps": gaps})
    if gap_meta is not None:
        meta["gap_report"] = gap_meta
    if (company and company != result.company) or (role and role != result.role):
        meta["extracted"] = {"company": result.company, "role": result.role}
    calls = result.usage.get("calls", []) + layout_meta.get("usage", {}).get("calls", []) + gap_calls
    meta["calls"] = calls  # one row per API call: step, model, effort, tokens, web searches, cost_usd
    meta["cost_usd"] = costs.totals(calls)["cost_usd"]
    meta["cost_by_step"] = costs.by_bucket(calls)
    _write_json(folder / "cv.json", cv)
    _write_json(folder / "cv.meta.json", meta)
    costs.append_csv(Path(output_root) / COSTS_CSV, folder.name, result.model, calls)
    log.info("done: %s (%d page(s), %d widow(s))", folder, layout_meta["final_pages"], layout_meta["final_widows"])
    return folder


# ---------------------------
# RERENDER
# ---------------------------
@dataclass
class Rerendered:
    pdf: Path
    report: LayoutReport
    validation_errors: list[str]


def rerender(folder: Path, *, profile_path: Path = DEFAULT_PROFILE, progress=None) -> Rerendered:
    """Re-render an output folder from its (hand-edited) cv.json, overwriting the docx/pdf in place.

    Runs validate() and the layout check and records them in cv.meta.json; validation errors
    are reported, not fatal, because the edit was deliberate. No API call.
    """
    progress = progress or (lambda message: None)
    folder = Path(folder)
    cv_path = folder / "cv.json"
    if not cv_path.is_file():
        draft = folder / "cv.draft.json"
        raise SetupError(f"No cv.json in {folder}" + (" (there is a cv.draft.json: fix it and save it as cv.json)" if draft.is_file() else ""))
    try:
        cv = json.loads(cv_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SetupError(f"{cv_path} is not valid JSON: {exc}") from None
    meta_path = folder / "cv.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    profile = load_profile_checked(profile_path)
    check_libreoffice()

    progress("Validating")
    errors = validate(cv, meta, profile)
    existing = sorted(p.stem for p in folder.glob("CV_*.pdf") if p.stem != ANON_BASENAME)
    basename = existing[0] if existing else f"CV_{candidate_filename()}"
    progress("Rendering (LibreOffice)")
    _, pdf = render(cv, folder, basename)
    if not Path(pdf).is_file():
        raise PipelineError(f"PDF was not created: {pdf}. Is LibreOffice already running headless?")
    report = check_layout(pdf, cv)
    progress(f"Layout check: {report.pages} page(s), {len(report.widows)} widow(s)")
    progress("Rendering the anonymized CV")
    render_anonymized(cv, folder)
    meta.setdefault("rerenders", []).append({
        "date": date.today().isoformat(), **report.summary(), "validation_errors": errors})
    _write_json(meta_path, meta)
    return Rerendered(Path(pdf), report, errors)
