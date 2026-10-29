"""The cvgen CLI, cost estimates, limit overrides and setup checks, all offline."""

import json
from pathlib import Path

import pymupdf
import pytest

from cvgen import cli, cost, limits, pipeline

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "profile.example.json"


# ---------------------------------------------------------------- cost and limits
def test_cost_estimate():
    usage = {"input_tokens": 702, "output_tokens": 9569, "cache_creation_input_tokens": 22583, "cache_read_input_tokens": 0}
    # 702*4 + 9569*20 + 22583*4*1.25 = 307,103 micro-dollars
    assert cost.estimate(usage, "claude-opus-5-5") == pytest.approx(0.307103, abs=1e-6)
    assert cost.estimate({"cache_read_input_tokens": 1_000_000}, "claude-opus-5-5") == pytest.approx(0.20)
    assert cost.estimate(usage, "some-unknown-model") is None
    meta = {"model": "claude-opus-5-5", "usage": usage, "layout": {"usage": {"input_tokens": 4314, "output_tokens": 580}}}
    assert cost.run_cost(meta) == pytest.approx(0.307103 + (4314 * 4 + 580 * 20) / 1e6, abs=1e-4)
    assert cost.fmt(0.4239) == "$0.42" and cost.fmt(None) == "n/a"


def test_limits_override_restores():
    with limits.override(MAX_PAGES=1):
        assert limits.MAX_PAGES == 1
    assert limits.MAX_PAGES == 2
    with pytest.raises(KeyError):
        with limits.override(NOT_A_LIMIT=3):
            pass


# ---------------------------------------------------------------- setup checks
def test_missing_api_key(monkeypatch):
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(pipeline.SetupError, match="ANTHROPIC_API_KEY is not set"):
        pipeline.check_api_key()


def test_missing_libreoffice(monkeypatch):
    def missing():
        raise RuntimeError("LibreOffice not found")
    monkeypatch.setattr(pipeline, "get_soffice_path", missing)
    with pytest.raises(pipeline.SetupError, match="LibreOffice .* was not found"):
        pipeline.check_libreoffice()


def test_invalid_profile(tmp_path):
    bad = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    del bad["experience"][0]["bullets"][0]["id"]
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(pipeline.SetupError, match=r"1 error\(s\):\n  - experience\[0\].bullets\[0\]: missing id"):
        pipeline.load_profile_checked(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(pipeline.SetupError, match="not valid JSON"):
        pipeline.load_profile_checked(path)
    with pytest.raises(pipeline.SetupError, match="Profile not found"):
        pipeline.load_profile_checked(tmp_path / "nope.json")


def test_setup_errors_print_clearly(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "load_profile_checked", lambda p: {})
    monkeypatch.setattr(cli, "check_api_key", lambda: (_ for _ in ()).throw(pipeline.SetupError("ANTHROPIC_API_KEY is not set.")))
    (tmp_path / "a.txt").write_text("JD", encoding="utf-8")
    assert cli.main(["batch", str(tmp_path)]) == 1
    assert "Error: ANTHROPIC_API_KEY is not set." in capsys.readouterr().err


# ---------------------------------------------------------------- commands
def fake_folder(root: Path, name: str, company: str, role: str, widows=0) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    meta = {"company": company, "role": role, "model": "claude-opus-5-5", "gaps": ["Go"],
            "usage": {"input_tokens": 700, "output_tokens": 9000, "cache_read_input_tokens": 22583},
            "layout": {"rounds": [{"round": 0, "pages": 2, "widows": [], "unmapped": []}], "kept_round": 0,
                       "final_pages": 2, "final_widows": widows, "usage": {}}, "cost_usd": 0.19}
    (folder / "cv.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "CV_AdrianMatei.pdf").write_bytes(b"%PDF")
    return folder


@pytest.fixture
def no_setup(monkeypatch):
    for name in ("load_profile_checked", "check_api_key", "check_libreoffice"):
        monkeypatch.setattr(cli, name, lambda *a, **k: None)


def test_new_from_file_passes_options(monkeypatch, tmp_path, capsys, no_setup):
    jd = tmp_path / "jd.txt"
    jd.write_text("Senior Backend Engineer at Acme", encoding="utf-8")
    calls = []

    def fake_run(text, company, role, **kwargs):
        calls.append((text, company, role, kwargs))
        kwargs["progress"]("Tailoring with claude-opus-5-5 (effort high)")
        return fake_folder(tmp_path / "out", "Acme_Backend_2026-10-29", "Acme", "Backend")
    monkeypatch.setattr(cli, "run", fake_run)
    code = cli.main(["new", "--jd-file", str(jd), "--company", "Acme", "--pages", "1", "--no-layout-fix", "--model", "claude-sonnet-5-5"])
    out = capsys.readouterr().out
    assert code == 0
    text, company, role, kwargs = calls[0]
    assert (text, company, role) == ("Senior Backend Engineer at Acme", "Acme", None)
    assert kwargs["pages"] == 1 and kwargs["layout_fix"] is False and kwargs["model"] == "claude-sonnet-5-5"
    assert "] Tailoring with claude-opus-5-5" in out
    assert "Estimated cost: $0.19" in out and "PDF:" in out and "CV_AdrianMatei.pdf" in out and "- Go" in out


def test_old_style_invocation_still_works(monkeypatch, tmp_path, no_setup):
    jd = tmp_path / "jd.txt"
    jd.write_text("JD", encoding="utf-8")
    seen = []
    monkeypatch.setattr(cli, "run", lambda text, c, r, **k: seen.append(text) or fake_folder(tmp_path / "o", "F", "A", "B"))
    assert cli.main([str(jd)]) == 0 and seen == ["JD"]


def test_batch_continues_after_a_failure(monkeypatch, tmp_path, capsys, no_setup):
    jds = tmp_path / "jds"
    jds.mkdir()
    for name, text in (("a_acme.txt", "acme"), ("b_broken.txt", "broken"), ("c_globex.txt", "globex")):
        (jds / name).write_text(text, encoding="utf-8")

    def fake_run(text, **kwargs):
        if text == "broken":
            raise pipeline.PipelineError("CV still invalid after 2 repair attempts (1 error(s)):\n  - summary: ...")
        return fake_folder(tmp_path / "out", f"{text.title()}_Engineer_2026-10-29", text.title(), "Engineer", widows=1)
    monkeypatch.setattr(cli, "run", fake_run)
    code = cli.main(["batch", str(jds)])
    out = capsys.readouterr().out
    assert code == 1
    assert "Summary: 2 succeeded, 1 failed, estimated total $0.38" in out
    table = out[out.index("JD  "):]
    assert "a_acme.txt" in table and "Acme_Engineer_2026-10-29" in table
    assert "b_broken.txt" in table and "FAILED: CV still invalid after 2 repair attempts" in table
    assert "c_globex.txt" in table and "Globex" in table  # ran after the failure
    for header in ("Company", "Role", "Folder", "Widows left", "Cost"):
        assert header in table


def test_profile_check_command(capsys):
    assert cli.main(["profile", "check", str(EXAMPLE)]) == 0
    assert "OK:" in capsys.readouterr().out


GOOD_CV = {
    "name": "Jane Doe", "headline": "Backend Engineer", "phone": "+40 700 000 000", "email": "jane.doe@example.com",
    "linkedinText": "linkedin.com/in/janedoe", "linkedinUrl": "https://www.linkedin.com/in/janedoe",
    "githubText": "github.com/janedoe", "githubUrl": "https://github.com/janedoe",
    "summary": "Backend engineer with 6 years building Node.js and TypeScript services for payments platforms.",
    "skills": [{"category": "Core", "skills": "Node.js, TypeScript"}],
    "experience": [{"title": "Software Engineer", "company": "Globex Logistics", "location": "Remote",
                    "period": "Jun 2020 – Feb 2023",
                    "bullets": ["Designed a PostgreSQL schema and REST API used by 300 warehouse clients"]}],
    "projects": [],
    "education": [{"institution": "Babes-Bolyai University", "degree": "BSc in Computer Science", "graduated": "Jun 2020"}],
    "certificates": [],
}
GOOD_META = {"sources": {
    "headline": ["headline-backend"], "summary": ["summary-backend"], "skills": [["skill-nodejs", "skill-typescript"]],
    "experience": [{"id": "exp-globex", "bullets": [["exp-globex-01"]]}], "projects": [], "education": ["edu-bsc"],
    "certificates": []}}


def test_rerender_overwrites_in_place(monkeypatch, tmp_path, capsys):
    folder = tmp_path / "Acme_Backend_2026-10-29"
    folder.mkdir()
    (folder / "CV_AdrianMatei.pdf").write_bytes(b"old")
    edited = json.loads(json.dumps(GOOD_CV))
    edited["experience"][0]["bullets"][0] = "Designed a PostgreSQL schema and REST API used by 450 warehouse clients"
    (folder / "cv.json").write_text(json.dumps(edited), encoding="utf-8")
    (folder / "cv.meta.json").write_text(json.dumps(GOOD_META), encoding="utf-8")
    rendered = []

    def fake_render(cv, out_dir, basename, template=None):
        rendered.append((cv, basename))
        pdf = Path(out_dir) / f"{basename}.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        for i, text in enumerate([cv["summary"], *cv["experience"][0]["bullets"]]):
            page.insert_text((40, 60 + 14 * i), text, fontname="helv", fontsize=9)
        doc.save(str(pdf))
        return Path(out_dir) / f"{basename}.docx", pdf
    monkeypatch.setattr(pipeline, "render", fake_render)
    monkeypatch.setattr(pipeline, "get_soffice_path", lambda: "soffice")

    assert cli.main(["rerender", str(folder), "--profile", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert rendered == [(edited, "CV_AdrianMatei")]  # same file name, overwritten in place
    assert (folder / "CV_AdrianMatei.pdf").read_bytes().startswith(b"%PDF")
    assert "1 validation issue(s) (rendered anyway)" in out and "450" in out
    assert "1 page(s), 0 widow(s)" in out
    meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
    assert len(meta["rerenders"]) == 1 and meta["rerenders"][0]["pages"] == 1
    assert "450" in meta["rerenders"][0]["validation_errors"][0]


def test_rerender_without_cv_json(tmp_path, capsys):
    (tmp_path / "cv.draft.json").write_text("{}", encoding="utf-8")
    assert cli.main(["rerender", str(tmp_path)]) == 1
    assert "there is a cv.draft.json" in capsys.readouterr().err
