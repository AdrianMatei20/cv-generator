"""Skill-gap report: validation, link rules, markdown, backlog, and the API loop with a fake client."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from cvgen import gaps, layout, pipeline
from cvgen.gaps import GapReport, gap_report, render_markdown, resolve_links, update_backlog, validate_report

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "profile.example.json"

REPORT = {
    "fit": {"score": 62, "justification": "Strong Node.js and PostgreSQL; no React or AWS.",
            "summary": "Jane matches the backend half well. The frontend and cloud requirements are gaps."},
    "requirements": [
        {"id": "req-1", "name": "Node.js", "skill": "Node.js", "category": "skill", "importance": "must-have",
         "status": "have", "evidence": ["exp-acme-01", "skill-nodejs"], "note": "Six years of Node.js services."},
        {"id": "req-2", "name": "React", "skill": "React", "category": "skill", "importance": "must-have",
         "status": "missing", "evidence": [], "note": "No frontend work in the profile."},
        {"id": "req-3", "name": "AWS (Lambda, S3)", "skill": "AWS", "category": "tool", "importance": "nice-to-have",
         "status": "partial", "evidence": ["cert-aws-saa"], "note": "Certification in preparation, no production use."},
    ],
    "recommendations": [
        {"requirement_ids": ["req-2"], "type": "project", "title": "React dashboard for StatusPage", "provider": "Self-directed",
         "why": "Shows React on top of an API the candidate already built.", "effort_hours": 16, "cost": "free",
         "project_idea": "Add a React + TypeScript dashboard to StatusPage that charts uptime from its PostgreSQL data.",
         "github": "README with screenshots, Vitest tests, a CI badge and a deployed demo.", "url": None},
        {"requirement_ids": ["req-3"], "type": "certification", "title": "AWS Certified Solutions Architect – Associate",
         "provider": "Amazon Web Services", "why": "Finishes the certification already in preparation.", "effort_hours": 60,
         "cost": "about $150 exam fee", "project_idea": None, "github": None,
         "url": "https://aws.amazon.com/certification/certified-solutions-architect-associate/"},
        {"requirement_ids": ["req-2"], "type": "course", "title": "React", "provider": "react.dev",
         "why": "The official tutorial.", "effort_hours": 8, "cost": "free", "project_idea": None, "github": None,
         "url": "https://react.dev/learn/made-up-from-memory"},
    ],
    "quick_wins": ["Book the AWS exam date", "Start the React dashboard", "Add a React line to the profile once shipped"],
}


@pytest.fixture
def profile():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def errors_for(profile, change):
    data = copy.deepcopy(REPORT)
    change(data)
    return validate_report(GapReport.model_validate(data), profile)


def test_good_report_validates(profile):
    assert validate_report(GapReport.model_validate(REPORT), profile) == []


def test_evidence_ids_must_exist_and_match_status(profile):
    def change(d):
        d["requirements"][0]["evidence"] = ["exp-acme-99"]
        d["requirements"][2]["evidence"] = []
        d["requirements"][1]["evidence"] = ["skill-python"]
    errors = errors_for(profile, change)
    assert "requirements[0] (Node.js): evidence id 'exp-acme-99' doesn't exist in profile.json" in errors
    assert any("requirements[2] (AWS (Lambda, S3)): status 'partial' needs evidence ids" in e for e in errors)
    assert any("requirements[1] (React): status 'missing' must have empty evidence" in e for e in errors)


def test_have_without_evidence_is_rejected(profile):
    errors = errors_for(profile, lambda d: d["requirements"][0].update(evidence=[]))
    assert any("status 'have' needs evidence ids" in e for e in errors)


def test_recommendations_must_point_to_gaps_and_cover_them(profile):
    def change(d):
        d["recommendations"][1]["requirement_ids"] = ["req-1"]  # Node.js is already "have"
        d["recommendations"][2]["requirement_ids"] = ["req-9"]
    errors = errors_for(profile, change)
    assert any("points to 'req-1' (Node.js), which the candidate already has" in e for e in errors)
    assert any("requirement id 'req-9' doesn't exist" in e for e in errors)
    assert "requirement 'req-3' (AWS (Lambda, S3)) is partial but no recommendation closes it" in errors


def test_projects_need_an_idea_and_quick_wins_max_three(profile):
    def change(d):
        d["recommendations"][0]["project_idea"] = None
        d["quick_wins"].append("A fourth one")
        d["fit"]["score"] = 120
    errors = errors_for(profile, change)
    assert any("a project needs project_idea and github" in e for e in errors)
    assert "quick_wins has 4 items; max 3" in errors and "fit.score is 120; it must be 0-100" in errors


def test_links_only_from_search_results():
    allowed = {gaps._norm_url("https://AWS.amazon.com/certification/certified-solutions-architect-associate")}
    resolved, dropped = resolve_links(REPORT, allowed)
    recs = resolved["recommendations"]
    assert recs[1]["url"].startswith("https://aws.amazon.com/") and recs[1]["search_url"] is None  # came from search
    assert recs[2]["url"] is None and dropped == ["https://react.dev/learn/made-up-from-memory"]  # written from memory
    assert recs[2]["search_url"] == "https://www.google.com/search?q=React+react.dev"
    assert recs[0]["url"] is None and "google.com/search" in recs[0]["search_url"]


def test_collect_result_urls_ignores_the_report_itself():
    contents = [[
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "aws saa"}},
        {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://aws.amazon.com/x/", "title": "t"}]},
        {"type": "text", "text": "ok", "citations": [{"type": "web_search_result_location", "url": "https://react.dev/learn"}]},
        {"type": "tool_use", "name": "submit_gap_report", "input": {"url": "https://invented.example.com"}},
    ]]
    assert gaps.collect_result_urls(contents) == {"https://aws.amazon.com/x", "https://react.dev/learn"}


def test_markdown_has_fit_table_groups_and_quick_wins():
    resolved, _ = resolve_links(REPORT, set())
    md = render_markdown(resolved, "Acme", "Full-Stack Engineer")
    assert "# Skill-gap report: Full-Stack Engineer at Acme" in md and "## Fit: 62/100" in md
    assert "## Requirements (1 have, 1 partial, 1 missing)" in md
    assert md.index("| React | must-have | **missing**") < md.index("| Node.js | must-have | **have**")  # gaps first
    assert "- **React** (missing, must-have): #1 React dashboard for StatusPage; #3 React" in md
    assert "- **AWS (Lambda, S3)** (partial, nice-to-have): #2 AWS Certified Solutions Architect" in md
    assert "### 1. Project: React dashboard for StatusPage" in md and "*Closes:* React" in md
    assert "*Build:* Add a React + TypeScript dashboard" in md and "[search](https://www.google.com/search?q=" in md
    assert "3. Add a React line to the profile once shipped" in md


def test_backlog_merges_and_counts(tmp_path):
    def write(folder, company, reqs):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "gap_report.json").write_text(json.dumps({"company": company, "requirements": reqs}), encoding="utf-8")
    req = lambda skill, status, imp="must-have": {"skill": skill, "status": status, "importance": imp}
    write("A_Dev_2026-10-01", "A", [req("React", "missing"), req("AWS", "partial", "nice-to-have"), req("Node.js", "have")])
    write("B_Dev_2026-10-02", "B", [req("react ", "missing"), req("AWS", "missing")])
    write("C_Dev_2026-10-03", "C", [req("Go", "missing", "nice-to-have"), req("React", "partial")])
    md = update_backlog(tmp_path).read_text(encoding="utf-8")
    lines = [l for l in md.splitlines() if l.startswith("| ") and not l.startswith("| Skill")]
    assert lines[0].startswith("| React | 2 | 1 | 3 | A (A_Dev_2026-10-01); B (B_Dev_2026-10-02); C (C_Dev_2026-10-03) |")
    assert lines[1].startswith("| AWS | 1 | 1 | 1 |") and lines[2].startswith("| Go | 1 | 0 | 0 |")
    assert "Node.js" not in md and "across 3 job description(s)" in md


# ---------------------------------------------------------------- API loop
class FakeClient:
    def __init__(self, messages):
        self.out = list(messages)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        message = self.out.pop(0)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: message)

            def __exit__(self_inner, *exc):
                return False
        return Ctx()


def msg(content, stop="tool_use", searches=0):
    usage = SimpleNamespace(input_tokens=900, output_tokens=3000, cache_creation_input_tokens=0, cache_read_input_tokens=20000,
                            server_tool_use=SimpleNamespace(web_search_requests=searches))
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage, model="claude-opus-5-5", stop_details=None)


def block(**kw):
    return SimpleNamespace(model_dump=lambda: kw, **kw)


def submit(report, call_id="toolu_g"):
    return block(type="tool_use", name="submit_gap_report", id=call_id, input=copy.deepcopy(report))


def test_gap_report_resumes_search_pause_repairs_and_filters_links(profile, monkeypatch):
    monkeypatch.setenv("GAP_WEB_SEARCH", "1")
    search = [block(type="server_tool_use", name="web_search", id="srv_1", input={"query": "aws saa"}),
              block(type="web_search_tool_result", content=[{"type": "web_search_result", "title": "AWS",
                    "url": "https://aws.amazon.com/certification/certified-solutions-architect-associate/"}])]
    bad = copy.deepcopy(REPORT)
    bad["requirements"][0]["evidence"] = []  # "have" without evidence
    client = FakeClient([msg(search, "pause_turn", searches=2), msg([submit(bad, "toolu_1")]), msg([submit(REPORT, "toolu_2")])])
    result = gap_report(profile, "JD", client=client)
    assert len(client.requests) == 3
    resumed = client.requests[1]["messages"]
    assert [m["role"] for m in resumed] == ["user", "system", "assistant"]  # resume: no extra user turn
    assert resumed[1]["content"][0] == {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "submit_cv"}}
    repair = client.requests[2]["messages"][-1]["content"][0]
    assert repair["is_error"] and repair["tool_use_id"] == "toolu_1" and "needs evidence ids" in repair["content"]
    assert result.repairs[0]["attempt"] == 1
    recs = result.report["recommendations"]
    assert recs[1]["url"].startswith("https://aws.amazon.com/")  # found by search: kept
    assert recs[2]["url"] is None and result.web_search["urls_dropped"] == ["https://react.dev/learn/made-up-from-memory"]
    assert result.web_search == {"enabled": True, "requests": 2, "urls_found": 1, "urls_dropped": result.web_search["urls_dropped"], "reused_from_cache": 0}
    assert result.usage["web_search_requests"] == 2 and len(result.usage["calls"]) == 3


def test_web_search_off(profile, monkeypatch):
    monkeypatch.setenv("GAP_WEB_SEARCH", "0")
    client = FakeClient([msg([submit(REPORT)])])
    result = gap_report(profile, "JD", client=client)
    request = client.requests[0]
    assert "web_search" not in json.dumps(request["tools"])
    assert "Always null" in request["system"][2]["text"]
    assert all(r["url"] is None and r["search_url"] for r in result.report["recommendations"])
    assert result.web_search["enabled"] is False


# ---------------------------------------------------------------- pipeline
def test_pipeline_writes_gap_report_and_backlog(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("GAP_WEB_SEARCH", "0")
    selection = {
        "company": "Acme", "role": "Full-Stack Engineer",
        "headline": {"text": "Backend Engineer", "source_ids": ["headline-backend"]},
        "summary": {"text": "Backend engineer with 6 years building Node.js services.", "source_ids": ["summary-backend"]},
        "skills": [{"category": "Core", "skills": "Node.js, TypeScript", "source_ids": ["skill-nodejs", "skill-typescript"]}],
        "experience": [{"job_id": "exp-acme", "bullets": [{"text": "Cut p95 latency from 900ms to 220ms", "source_ids": ["exp-acme-02"]}]},
                       {"job_id": "exp-globex", "bullets": [{"text": "Designed a PostgreSQL schema for 300 clients", "source_ids": ["exp-globex-01"]}]}],
        "projects": [], "education_ids": ["edu-bsc"], "certification_ids": ["cert-cka"], "gaps": ["React"],
    }
    client = FakeClient([msg([block(type="tool_use", name="submit_cv", id="toolu_cv", input=selection)]), msg([submit(REPORT)])])

    def fake_render(cv, out_dir, basename, template=None):
        doc = pymupdf.open()
        page = doc.new_page()
        for i, text in enumerate([cv["summary"], *(b for e in cv["experience"] for b in e["bullets"])]):
            page.insert_text((40, 60 + 14 * i), text, fontname="helv", fontsize=9)
        doc.save(str(Path(out_dir) / f"{basename}.pdf"))
        return Path(out_dir) / f"{basename}.docx", Path(out_dir) / f"{basename}.pdf"
    monkeypatch.setattr(layout, "render", fake_render)
    monkeypatch.setattr(pipeline, "render", fake_render)

    folder = pipeline.run("Full-stack JD: Node.js, React, AWS", profile_path=EXAMPLE, output_root=tmp_path, client=client)
    assert {"gap_report.json", "gap_report.md"} <= {p.name for p in folder.iterdir()}
    assert (tmp_path / "skills_backlog.md").read_text(encoding="utf-8").count("| React | 1 |") == 1
    meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
    assert meta["gap_report"]["fit"] == 62 and meta["settings"]["gaps"] is True
    assert meta["cost_usd"] > 0
    assert client.requests[0]["tools"] == client.requests[1]["tools"]  # one cache prefix for both calls


# ---------------------------------------------------------------- recommendations cache
def test_cache_saves_reuses_and_expires(tmp_path, profile):
    from datetime import date
    path = tmp_path / "cache.json"
    resolved, _ = resolve_links(REPORT, {gaps._norm_url(REPORT["recommendations"][1]["url"])})
    assert gaps.save_to_cache(resolved, path, today=date(2026, 10, 1)) == 2  # projects aren't cached
    cache = gaps.load_cache(path, today=date(2026, 10, 29))
    assert set(cache) == {"aws", "react"} and cache["aws"]["recommendations"][0]["title"].startswith("AWS Certified")
    options = gaps.cached_options(cache)
    assert set(options) == {"aws-1", "react-1"}
    assert gaps.load_cache(path, today=date(2027, 1, 1)) == {}  # older than 90 days

    reuse = copy.deepcopy(REPORT)
    reuse["recommendations"][1] = {"requirement_ids": ["req-3"], "cached": "aws-1"}
    assert validate_report(GapReport.model_validate(reuse), profile, options) == []
    expanded = gaps.expand_cached(resolve_links(GapReport.model_validate(reuse).model_dump(), set())[0], options)
    rec = expanded["recommendations"][1]
    assert rec["title"].startswith("AWS Certified") and rec["url"].startswith("https://aws.amazon.com/")
    assert rec["requirement_ids"] == ["req-3"] and rec["search_url"] is None


def test_unknown_cached_reference_is_an_error(profile):
    bad = copy.deepcopy(REPORT)
    bad["recommendations"][1] = {"requirement_ids": ["req-3"], "cached": "kubernetes-9"}
    errors = validate_report(GapReport.model_validate(bad), profile, {})
    assert any("cached id 'kubernetes-9' isn't one of the saved recommendations" in e for e in errors)


def test_prompt_lists_saved_recommendations_and_search_rule():
    options = {"aws-1": {"skill": "AWS", "type": "certification", "title": "AWS SAA", "provider": "Amazon Web Services"}}
    text = gaps.gap_prompt(True, options)
    assert "- aws-1: [AWS] certification: AWS SAA (Amazon Web Services)" in text
    assert "Skip searching only when every must-have" in text and "up to 3 searches" in text
