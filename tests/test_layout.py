"""Layout detection on real and synthetic PDFs, and the fix loop offline (fake renderer + fake Claude)."""

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from cvgen import layout, limits
from cvgen.layout import check_layout, fix_layout
from cvgen.validate import validate

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "profile.example.json"


def write_pdf(path: Path, pages: list[list[str]], line_height=14, x=40, y0=60) -> Path:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for i, text in enumerate(lines):
            page.insert_text((x, y0 + i * line_height), text, fontname="helv", fontsize=10)
    doc.save(str(path))
    return path


# ---------------------------------------------------------------- detection
def test_detects_a_known_widow(tmp_path):
    cv = {
        "summary": "Backend engineer with six years of Node.js.",
        "experience": [{"bullets": [
            "Cut p95 latency of the merchant API from 900ms to 220ms by adding Redis caching and fixing slow queries",
            "Implemented the full authentication flow with email confirmation",
            "Shipped a mock API early",
        ]}],
        "projects": [{"bullets": ["Wrote unit tests", "This bullet is not in the PDF at all"]}],
    }
    pdf = write_pdf(tmp_path / "cv.pdf", [[
        "Summary",
        "Backend engineer with six years   of Node.js.",
        "Experience",
        "• Cut p95 latency of the merchant API from 900ms to 220ms by adding Redis caching and fixing",
        "slow queries",                                                   # 2 words alone: widow
        "• Implemented the full authentication flow with email con-",  # hyphenated at the break
        "firmation",                                                      # 1 word, but the bullet's last line
        "• Shipped a mock API early",
        "Projects",
        "• Wrote unit tests",
    ]])
    report = check_layout(pdf, cv)
    assert report.unmapped == ["projects[0].bullets[1]"]
    assert [w.path for w in report.widows] == ["experience[0].bullets[0]", "experience[0].bullets[1]"]
    widow = report.widows[0]
    assert len(widow.lines) == 2 and widow.last_line_words == 2
    assert report.placements["experience[0].bullets[2]"].last_line_words == 5  # one line: not a widow
    assert report.pages == 1 and not report.over_pages


def test_text_normalisation_handles_ligatures_glyphs_and_spacing():
    # PDF text can carry ligature characters, soft hyphens and odd spacing; the built-in test font
    # can't draw ligatures, so the normaliser is tested directly.
    from cvgen.layout import _compact, _strip_glyph
    assert _compact("Conﬁgured  ﬂows and eﬃcient­ caching") == _compact("Configured flows and efficient caching")
    assert _strip_glyph("●  Built a dashboard ") == "Built a dashboard"


def test_widow_threshold_is_a_setting(tmp_path):
    cv = {"summary": "", "experience": [{"bullets": ["Built a configurable tile dashboard for a construction platform team"]}]}
    pdf = write_pdf(tmp_path / "cv.pdf", [["• Built a configurable tile dashboard for a construction", "platform team"]])
    assert len(check_layout(pdf, cv).widows) == 1
    assert check_layout(pdf, cv, widow_max_words=1).widows == []


def test_flags_page_overflow(tmp_path, monkeypatch):
    cv = {"summary": "Short summary here.", "experience": [{"bullets": ["First bullet on page one", "Second bullet on page two"]}]}
    pdf = write_pdf(tmp_path / "cv.pdf", [["Short summary here.", "• First bullet on page one"], ["• Second bullet on page two"]])
    monkeypatch.setattr(limits, "MAX_PAGES", 1)
    report = check_layout(pdf, cv)
    assert report.over_pages and report.pages == 2 and report.lines_over == 1


def test_real_libreoffice_pdf_golden():
    """The golden legacy PDF (LibreOffice, Calibri, ● glyphs): every bullet maps; widows are pinned."""
    cv = json.loads((REPO / "tests/fixtures/data/sample.json").read_text(encoding="utf-8"))
    report = check_layout(REPO / "tests/golden/CV_AdrianMatei.pdf", cv)
    assert report.unmapped == [] and len(report.placements) == 31
    assert report.pages == 3
    assert [w.path for w in report.widows] == [
        "experience[0].bullets[0]", "experience[0].bullets[1]", "experience[0].bullets[6]", "experience[2].bullets[3]",
        "projects[0].bullets[1]", "projects[0].bullets[3]", "projects[0].bullets[4]"]
    assert report.placements["experience[0].bullets[0]"].lines[-1].text.strip() == "monorepo"


# ---------------------------------------------------------------- fix loop
WIDOW = "Cut p95 latency of the merchant API from 900ms to 220ms with Redis caching and fixing N+1 PostgreSQL queries"
FIXED = "Cut p95 latency of the merchant API from 900ms to 220ms with Redis caching and N+1 query fixes"


@pytest.fixture
def profile():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def cv_and_sources():
    cv = {
        "name": "Jane Doe", "headline": "Backend Engineer", "phone": "+40 700 000 000", "email": "jane.doe@example.com",
        "linkedinText": "linkedin.com/in/janedoe", "linkedinUrl": "https://www.linkedin.com/in/janedoe",
        "githubText": "github.com/janedoe", "githubUrl": "https://github.com/janedoe",
        "summary": "Backend engineer with 6 years building Node.js and TypeScript services for payments platforms.",
        "skills": [{"category": "Core", "skills": "Node.js, TypeScript"}],
        "experience": [
            {"title": "Senior Backend Engineer", "company": "Acme Payments", "location": "Hybrid", "period": "Mar 2023 – Present",
             "bullets": [WIDOW, "Led root cause analysis for 12 severity-1 incidents"]},
            {"title": "Software Engineer", "company": "Globex Logistics", "location": "Remote", "period": "Jun 2020 – Feb 2023",
             "bullets": ["Designed a PostgreSQL schema and REST API used by 300 warehouse clients",
                         "Migrated 14 cron jobs to Kubernetes CronJobs with alerting"]},
        ],
        "projects": [],
        "education": [{"institution": "Babes-Bolyai University", "degree": "BSc in Computer Science", "graduated": "Jun 2020"}],
        "certificates": [{"certificate": "Certified Kubernetes Administrator (CKA)", "date": "May 2024"}],
    }
    sources = {
        "headline": ["headline-backend"], "summary": ["summary-backend"], "skills": [["skill-nodejs", "skill-typescript"]],
        "experience": [{"id": "exp-acme", "bullets": [["exp-acme-02"], ["exp-acme-03"]]},
                       {"id": "exp-globex", "bullets": [["exp-globex-01"], ["exp-globex-02"]]}],
        "projects": [], "education": ["edu-bsc"], "certificates": ["cert-cka"],
    }
    return cv, sources


@pytest.fixture
def fake_render(monkeypatch):
    """Render by wrapping text at 100 characters, `lines_per_page` lines per page."""
    state = {"lines_per_page": 40, "calls": []}

    def render(cv, out_dir, basename, template=None):
        lines = ["Summary", *textwrap.wrap(cv["summary"], 100)]
        for section in ("experience", "projects"):
            for entry in cv[section]:
                lines.append(entry.get("company") or entry.get("name"))
                for bullet in entry["bullets"]:
                    wrapped = textwrap.wrap(bullet, 100)
                    lines += ["• " + wrapped[0], *wrapped[1:]]
        n = state["lines_per_page"]
        out = Path(out_dir)
        write_pdf(out / f"{basename}.pdf", [lines[i:i + n] for i in range(0, len(lines), n)])
        (out / f"{basename}.docx").write_bytes(b"docx")
        state["calls"].append(json.loads(json.dumps(cv)))
        return out / f"{basename}.docx", out / f"{basename}.pdf"
    monkeypatch.setattr(layout, "render", render)
    return state


class FakeClient:
    def __init__(self, plans):
        self.plans = list(plans)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        call = SimpleNamespace(type="tool_use", name="rewrite_bullets", id="toolu_l", input=self.plans.pop(0))
        usage = SimpleNamespace(input_tokens=300, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        message = SimpleNamespace(content=[call], stop_reason="tool_use", usage=usage, model="claude-opus-5-5", stop_details=None)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: message)

            def __exit__(self_inner, *exc):
                return False
        return Ctx()


def test_fix_layout_rewrites_a_widow(tmp_path, profile, cv_and_sources, fake_render):
    cv, sources = cv_and_sources
    assert validate(cv, {"sources": sources}, profile) == []
    client = FakeClient([{"rewrites": [{"path": "experience[0].bullets[0]", "text": FIXED}], "remove": []}])
    final_cv, final_sources, meta = fix_layout(cv, sources, profile, "JD text", tmp_path, "CV_X", client=client)
    assert final_cv["experience"][0]["bullets"][0] == FIXED
    assert meta["kept_round"] == 1 and meta["final_widows"] == 0 and meta["final_pages"] == 1
    assert [r["round"] for r in meta["rounds"]] == [0, 1]
    assert meta["rounds"][0]["widows"][0]["path"] == "experience[0].bullets[0]"
    assert meta["rounds"][1]["changes"] == [{"path": "experience[0].bullets[0]", "before": WIDOW, "after": FIXED}]
    prompt = client.requests[0]["messages"][0]["content"]
    assert "[exp-acme-02] Cut p95 latency" in prompt and "last line holds 1 word(s)" in prompt  # sources sent, so no inventing
    assert fake_render["calls"][-1] == final_cv  # files on disk are the kept version
    assert meta["usage"]["output_tokens"] == 200


def test_fix_layout_rejects_invented_numbers_and_keeps_best(tmp_path, profile, cv_and_sources, fake_render):
    cv, sources = cv_and_sources
    invented = "Cut p95 latency of the merchant API from 900ms to 150ms with Redis caching"
    client = FakeClient([{"rewrites": [{"path": "experience[0].bullets[0]", "text": invented}], "remove": []},
                         {"rewrites": [{"path": "experience[0].bullets[0]", "text": WIDOW + " in production"}], "remove": []}])
    final_cv, _, meta = fix_layout(cv, sources, profile, "JD", tmp_path, "CV_X", client=client)
    assert meta["rounds"][1]["result"] == "rejected" and "150" in meta["rounds"][1]["validation_errors"][0]
    assert meta["rounds"][2]["result"] == "worse, discarded"  # still a widow, so not better than round 0
    assert meta["kept_round"] == 0 and final_cv == cv
    assert fake_render["calls"][-1] == cv  # re-rendered the kept version after the discarded round


def test_fix_layout_trims_when_over_pages(tmp_path, profile, cv_and_sources, fake_render, monkeypatch):
    cv, sources = cv_and_sources
    cv["experience"][0]["bullets"][0] = FIXED  # no widows; only the page overflow
    monkeypatch.setattr(limits, "MAX_PAGES", 1)
    fake_render["lines_per_page"] = 7  # 8 lines -> 2 pages
    client = FakeClient([{"rewrites": [], "remove": ["experience[1].bullets[1]"]}])
    final_cv, final_sources, meta = fix_layout(cv, sources, profile, "JD", tmp_path, "CV_X", client=client)
    assert final_cv["experience"][1]["bullets"] == ["Designed a PostgreSQL schema and REST API used by 300 warehouse clients"]
    assert final_sources["experience"][1]["bullets"] == [["exp-globex-01"]]
    assert meta["rounds"][0]["over_pages"] and meta["final_pages"] == 1 and meta["kept_round"] == 1
    assert "All bullets, for choosing what to remove" in client.requests[0]["messages"][0]["content"]


def test_fix_layout_does_nothing_when_clean(tmp_path, profile, cv_and_sources, fake_render):
    cv, sources = cv_and_sources
    cv["experience"][0]["bullets"][0] = FIXED
    final_cv, _, meta = fix_layout(cv, sources, profile, "JD", tmp_path, "CV_X", client=FakeClient([]))
    assert final_cv == cv and meta["rounds"] == [{"round": 0, **meta["rounds"][0]}] and meta["usage"] == {"calls": []}
    assert len(fake_render["calls"]) == 1
