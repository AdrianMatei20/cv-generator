"""Output naming and the end-to-end pipeline, offline (fake Claude client, stubbed render)."""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from cvgen import layout, pipeline
from cvgen.output import candidate_filename, create_output_dir, folder_name, sanitize_part
from cvgen.tailor import MAX_REPAIRS

EXAMPLE = Path(__file__).resolve().parent.parent / "profile.example.json"
DAY = date(2026, 10, 26)


@pytest.mark.parametrize("raw, expected", [
    ("Google", "Google"),
    ("software engineer", "SoftwareEngineer"),
    ("Senior Technical Support Engineer – L3", "SeniorTechnicalSupportEngineerL3"),
    ('Back\\end/Dev: "Lead" *now*? <x>|y', "BackEndDevLeadNowXY"),
    ("Société Générale", "SocieteGenerale"),
    ("Zürich Ünïcode Straße", "ZurichUnicodeStraße"),
    ("snake_case-and.dots", "SnakeCaseAndDots"),
    ("x" * 60, "X" + "x" * 39),
    ("", "Unknown"),
    (None, "Unknown"),
    ("?!*", "Unknown"),
    ("con", "Unknown"),
])
def test_sanitize_part(raw, expected):
    assert sanitize_part(raw) == expected
    assert len(sanitize_part(raw)) <= 40


def test_folder_name_and_suffixes(tmp_path):
    assert folder_name("Google", "Software Engineer", DAY) == "Google_SoftwareEngineer_2026-10-26"
    first = create_output_dir(tmp_path, "Google", "Software Engineer", DAY)
    second = create_output_dir(tmp_path, "Google", "Software Engineer", DAY)
    third = create_output_dir(tmp_path, "Google", "Software Engineer", DAY)
    assert [p.name for p in (first, second, third)] == [
        "Google_SoftwareEngineer_2026-10-26", "Google_SoftwareEngineer_2026-10-26_2", "Google_SoftwareEngineer_2026-10-26_3"]


def test_candidate_filename_setting(monkeypatch):
    monkeypatch.delenv("CANDIDATE_FILENAME", raising=False)
    assert candidate_filename() == "AdrianMatei"
    monkeypatch.setenv("CANDIDATE_FILENAME", "Jane Doe")
    assert candidate_filename() == "JaneDoe"


# ---------------------------------------------------------------- pipeline
SELECTION = {
    "company": "Acme Payments", "role": "Senior Backend Engineer",
    "headline": {"text": "Backend Engineer – Node.js / TypeScript", "source_ids": ["headline-backend"]},
    "summary": {"text": "Backend engineer with 6 years building Node.js services.", "source_ids": ["summary-backend"]},
    "skills": [{"category": "Core", "skills": "Node.js, TypeScript", "source_ids": ["skill-nodejs", "skill-typescript"]}],
    "experience": [
        {"job_id": "exp-acme", "bullets": [{"text": "Cut p95 latency from 900ms to 220ms", "source_ids": ["exp-acme-02"]}]},
        {"job_id": "exp-globex", "bullets": [{"text": "Designed a PostgreSQL schema for 300 clients", "source_ids": ["exp-globex-01"]}]},
    ],
    "projects": [],
    "education_ids": ["edu-bsc"], "certification_ids": ["cert-cka"], "gaps": [],
}


class FakeClient:
    def __init__(self, inputs):
        self.inputs = list(inputs)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        call = SimpleNamespace(type="tool_use", name="submit_cv", id="toolu_x", input=self.inputs.pop(0))
        usage = SimpleNamespace(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        message = SimpleNamespace(content=[call], stop_reason="tool_use", usage=usage, model="claude-opus-5-5", stop_details=None)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: message)

            def __exit__(self_inner, *exc):
                return False
        return Ctx()


@pytest.fixture
def rendered(monkeypatch):
    """Stub render(): record what it was given and write a one-page PDF, one line per item (no widows)."""
    calls = []

    def fake_render(cv, out_dir, basename, template=None):
        calls.append((json.loads(json.dumps(cv)), Path(out_dir), basename))
        docx, pdf = Path(out_dir) / f"{basename}.docx", Path(out_dir) / f"{basename}.pdf"
        docx.write_bytes(b"docx")
        doc = pymupdf.open()
        page = doc.new_page()
        lines = [cv["summary"]] + [b for section in ("experience", "projects") for e in cv[section] for b in e["bullets"]]
        for i, text in enumerate(lines):
            page.insert_text((40, 60 + 14 * i), text, fontname="helv", fontsize=9)
        doc.save(str(pdf))
        return docx, pdf
    monkeypatch.setattr(layout, "render", fake_render)
    monkeypatch.setattr(pipeline, "render", fake_render)  # the anonymized copy
    monkeypatch.delenv("CANDIDATE_FILENAME", raising=False)
    return calls


def run_example(tmp_path, jd="Senior Backend Engineer at Acme.\r\nNode.js required.\n", **kwargs):
    return pipeline.run(jd, profile_path=EXAMPLE, output_root=tmp_path, today=DAY, gaps=False,
                        client=FakeClient([json.loads(json.dumps(SELECTION))]), **kwargs)


def test_run_writes_the_output_folder(tmp_path, rendered):
    folder = run_example(tmp_path)
    assert folder == tmp_path / "AcmePayments_SeniorBackendEngineer_2026-10-26"
    assert sorted(p.name for p in folder.iterdir()) == [
        "CV_AdrianMatei.docx", "CV_AdrianMatei.pdf", "CV_JohnDoe.docx", "CV_JohnDoe.pdf",
        "cv.json", "cv.meta.json", "job_description.txt"]
    cv = json.loads((folder / "cv.json").read_text(encoding="utf-8"))
    assert cv == rendered[0][0]  # cv.json is exactly what was rendered
    assert rendered[0][1:] == (folder, "CV_AdrianMatei")
    anon, anon_folder, anon_name = rendered[-1]  # the final CV, anonymized like generate.py does
    assert (anon_folder, anon_name) == (folder, "CV_JohnDoe")
    assert anon["name"] == "John Doe" and anon["email"] == "email@example.com" and anon["phone"] == "0000000000"
    assert {j["company"] for j in anon["experience"]} == {"Company Name"}
    assert [j["bullets"] for j in anon["experience"]] == [j["bullets"] for j in cv["experience"]]
    assert "jane" not in json.dumps(anon).lower()
    meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
    assert meta["model"] == "claude-opus-5-5" and meta["sources"]["experience"][0]["id"] == "exp-acme"
    assert meta["usage"]["output_tokens"] == 20 and "extracted" not in meta
    assert meta["layout"]["final_pages"] == 1 and meta["layout"]["final_widows"] == 0 and meta["layout"]["rounds"][0]["round"] == 0
    assert (folder / "job_description.txt").read_bytes() == "Senior Backend Engineer at Acme.\r\nNode.js required.\n".encode()


def test_run_explicit_company_and_role_win(tmp_path, rendered):
    folder = run_example(tmp_path, company="Google", role="Site Reliability Engineer")
    assert folder.name == "Google_SiteReliabilityEngineer_2026-10-26"
    meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
    assert (meta["company"], meta["role"]) == ("Google", "Site Reliability Engineer")
    assert meta["extracted"] == {"company": "Acme Payments", "role": "Senior Backend Engineer"}


def test_run_never_overwrites(tmp_path, rendered):
    assert run_example(tmp_path).name.endswith("2026-10-26")
    assert run_example(tmp_path).name.endswith("2026-10-26_2")


def test_run_fails_when_pdf_missing(tmp_path, monkeypatch, rendered):
    monkeypatch.setattr(layout, "render", lambda cv, out, base, template=None: (Path(out) / "x.docx", Path(out) / "x.pdf"))
    with pytest.raises(pipeline.PipelineError, match="PDF was not created"):
        run_example(tmp_path)


def test_run_saves_draft_when_repairs_fail(tmp_path, rendered):
    bad = json.loads(json.dumps(SELECTION))
    bad["experience"][1]["bullets"][0]["text"] = "Designed a PostgreSQL schema for 450 clients"
    client = FakeClient([json.loads(json.dumps(bad)) for _ in range(MAX_REPAIRS + 1)])
    with pytest.raises(pipeline.PipelineError, match="Draft saved in") as info:
        pipeline.run("JD", profile_path=EXAMPLE, output_root=tmp_path, today=DAY, client=client)
    folder = tmp_path / "AcmePayments_SeniorBackendEngineer_2026-10-26"
    assert str(folder) in str(info.value)
    assert sorted(p.name for p in folder.iterdir()) == ["cv.draft.json", "cv.draft.meta.json", "job_description.txt"]
    assert rendered == []
