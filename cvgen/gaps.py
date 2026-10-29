"""Skill-gap report for a JD: how well the profile fits, what's missing, and how to close it.

    result = gap_report(profile, jd, client=client)
    write_gap_report(folder, result, company, role)   # gap_report.json + gap_report.md
    update_backlog(output_root)                       # output/skills_backlog.md

The call shares the tailoring call's cached prefix: same tools (submit_gap_report and
web_search are declared deferred in cvgen.tailor.shared_tools), same betas, same shared
rules and profile. Only the task prompt after the cache breakpoint differs, and a
mid-conversation system message swaps submit_cv out and the gap tools in.

Links: with GAP_WEB_SEARCH on (default), Claude may use Anthropic's server-side web search,
and a recommendation keeps its url only if that exact address came back in the search
results. Otherwise url is null and the report links to a web search for "<title> <provider>".
"""

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote_plus, urlsplit, urlunsplit

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cvgen import tailor as _tailor
from cvgen.profile import check_profile
from cvgen.tailor import DEFAULT_EFFORT, DEFAULT_MODEL, MAX_REPAIRS, TailorError, _usage, strict_schema

TOOL_NAME = "submit_gap_report"
MAX_TOKENS = 32000
MAX_SEARCHES = 6
MAX_PAUSES = 3  # resumes of a server-tool loop that hit its iteration limit (stop_reason "pause_turn")

log = logging.getLogger(__name__)


# ---------------------------
# TOOL INPUT
# ---------------------------
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fit(_Model):
    score: int = Field(description="0-100. Must-haves weigh far more than nice-to-haves.")
    justification: str = Field(description="One line.")
    summary: str = Field(description="2-3 sentences on how well the candidate matches this JD.")


class Requirement(_Model):
    id: str = Field(description="Short unique id within this report, e.g. 'req-1'.")
    name: str = Field(description="The requirement as the JD states it.")
    skill: str = Field(description="Short canonical label to track this across JDs, e.g. 'AWS', 'React', '6+ years backend'.")
    category: Literal["skill", "tool", "experience", "domain", "certification", "education", "soft-skill", "language", "other"]
    importance: Literal["must-have", "nice-to-have"]
    status: Literal["have", "partial", "missing"]
    evidence: list[str] = Field(description="Profile ids that prove it; empty when missing.")
    note: str = Field(description="One short sentence explaining the status.")


class Recommendation(_Model):
    requirement_ids: list[str] = Field(description="Ids of the partial/missing requirements this closes.")
    type: Literal["course", "certification", "project", "reading", "practice"]
    title: str
    provider: str
    why: str = Field(description="One sentence on why this closes the gap.")
    effort_hours: float
    cost: str = Field(description="'free' or an approximate price with currency.")
    project_idea: Optional[str] = Field(description="Projects only: what to build in 1-2 weekends, combining the gap with existing skills.")
    github: Optional[str] = Field(description="Projects only: what to put in the repo to show it.")
    url: Optional[str] = Field(description="Only an address that appeared in web search results; otherwise null.")


class GapReport(_Model):
    fit: Fit
    requirements: list[Requirement]
    recommendations: list[Recommendation] = Field(description="Biggest improvement to fit first.")
    quick_wins: list[str] = Field(description="Up to 3 things to do this week.")


# Not "strict": it shares the request with strict submit_cv, and two strict schemas this size
# exceed the API's grammar limit. GapReport.model_validate + the repair loop catch shape errors.
GAP_TOOL = {
    "name": TOOL_NAME,
    "description": "Submit the skill-gap report for the JD. Call exactly once, with the complete report.",
    "input_schema": strict_schema(GapReport),
    "defer_loading": True,  # declared in every profile call, surfaced only by the gap call
}
# allowed_callers: this version is code-execution-only by default when surfaced from deferred.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES,
                   "defer_loading": True, "allowed_callers": ["direct"]}


# ---------------------------
# PROMPT AND REQUEST
# ---------------------------
def gap_prompt(web_search: bool) -> str:
    url_rule = (
        f"Use the web_search tool (at most {MAX_SEARCHES} searches; one search can cover several recommendations, "
        "so start with the top ones) to find each recommendation's official page. Put in `url` only an address that "
        "appeared in your search results; if you found none, use null. Never write a URL from memory."
        if web_search else
        "Always null. The application adds links itself; never write a URL from memory."
    )
    return f"""\
Task: write an honest skill-gap report for this JD. Submit it by calling the {TOOL_NAME} tool exactly once; don't answer in plain text.

Requirements
- List every requirement in the JD: skills, tools, years of experience, domain knowledge, certifications, education, soft skills, languages. One entry per distinct requirement; split combined ones ("Kubernetes and Terraform" is two).
- importance: "must-have" when the JD says required, must, need, or "X+ years", or lists it under requirements; "nice-to-have" for plus, preferred, bonus or nice to have.
- status: "have" only when profile items clearly show it; "partial" when the profile shows related or lesser experience (fewer years than asked, personal projects but no job use, a neighbouring tool); "missing" when nothing in the profile supports it. Cite the profile ids in `evidence` for "have" and "partial"; leave it empty for "missing". Never mark "have" or "partial" without evidence. For years of experience, work them out from the job dates.
- skill: a short canonical label for tracking this requirement across many JDs, the same label you would use for it in any JD ("AWS", "React", "Kubernetes", "Payments domain", "6+ years backend").
- note: one short sentence explaining the status.

Fit
- score 0-100, weighting must-haves far above nice-to-haves: 90+ meets every must-have; under 50 misses several. justification: one line. summary: 2-3 sentences.

Recommendations
- Every partial or missing requirement gets at least one recommendation that closes it; one recommendation may cover several requirements (list all their ids). Never recommend something the candidate already has.
- Order recommendations by how much they would improve the candidate's fit for this job, biggest first.
- type: course, certification, project, reading or practice. Prefer reputable sources: official vendor certifications and documentation, major course platforms (Coursera, edX, Udemy, Pluralsight, freeCodeCamp), official tutorials.
- effort_hours: a realistic estimate. cost: "free" or an approximate price with currency (e.g. "about $150 exam fee").
- For a project: `project_idea` is a concrete thing to build in 1-2 weekends that combines the missing skill with skills the candidate already has (name them), and `github` says what to put in the repo to show it (README with an architecture diagram, tests, a CI badge, a deployed demo). For other types set both to null.
- url: {url_rule}

Quick wins: up to 3 things the candidate could do this week that would most improve their fit for similar jobs.

The candidate reads this report: write to them in the second person ("you", "your") in every field. Plain text; no markdown.
"""


def build_gap_request(profile: dict, jd: str, model: str, effort: str, web_search: bool) -> dict:
    """Same tools, betas, shared rules and profile as the tailoring call (so the prefix is a
    cache hit); the task prompt and a tool swap come after the breakpoint."""
    changes = [{"type": "tool_removal", "tool": {"type": "tool_reference", "name": _tailor.TOOL_NAME}},
               {"type": "tool_addition", "tool": {"type": "tool_reference", "name": TOOL_NAME}}]
    if web_search:
        changes.append({"type": "tool_addition", "tool": {"type": "tool_reference", "name": "web_search"}})
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "betas": _tailor.BETAS,
        "fallbacks": "default",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "tools": _tailor.shared_tools(),
        "tool_choice": {"type": "auto"},
        "system": _tailor.shared_system(profile, gap_prompt(web_search)),
        "messages": [
            {"role": "user", "content": f"<job_description>\n{jd.strip()}\n</job_description>\n\n"
                                        f"Write the skill-gap report for this job and submit it with {TOOL_NAME}."},
            {"role": "system", "content": changes},
        ],
    }


# ---------------------------
# VALIDATION AND LINKS
# ---------------------------
def validate_report(report: GapReport, profile: dict) -> list[str]:
    errors = []
    index = check_profile(profile).profile.index()
    if not 0 <= report.fit.score <= 100:
        errors.append(f"fit.score is {report.fit.score}; it must be 0-100")
    for key in ("justification", "summary"):
        if not getattr(report.fit, key).strip():
            errors.append(f"fit.{key} is empty")
    by_id = {}
    for i, req in enumerate(report.requirements):
        where = f"requirements[{i}] ({req.name})"
        if not req.id.strip() or req.id in by_id:
            errors.append(f"{where}: id '{req.id}' is empty or used twice")
        by_id[req.id] = req
        for item_id in req.evidence:
            if item_id not in index:
                errors.append(f"{where}: evidence id '{item_id}' doesn't exist in profile.json")
        if req.status in ("have", "partial") and not req.evidence:
            errors.append(f"{where}: status '{req.status}' needs evidence ids; use 'missing' if the profile has none")
        if req.status == "missing" and req.evidence:
            errors.append(f"{where}: status 'missing' must have empty evidence (or use 'partial')")
    if not report.requirements:
        errors.append("requirements is empty; list every requirement in the JD")
    covered = set()
    for i, rec in enumerate(report.recommendations):
        where = f"recommendations[{i}] ({rec.title})"
        if not rec.requirement_ids:
            errors.append(f"{where}: requirement_ids is empty")
        for req_id in rec.requirement_ids:
            req = by_id.get(req_id)
            if req is None:
                errors.append(f"{where}: requirement id '{req_id}' doesn't exist")
            elif req.status == "have":
                errors.append(f"{where}: points to '{req_id}' ({req.name}), which the candidate already has")
            else:
                covered.add(req_id)
        for key in ("title", "provider", "why", "cost"):
            if not getattr(rec, key).strip():
                errors.append(f"{where}: {key} is empty")
        if rec.effort_hours <= 0:
            errors.append(f"{where}: effort_hours must be positive")
        if rec.type == "project" and not ((rec.project_idea or "").strip() and (rec.github or "").strip()):
            errors.append(f"{where}: a project needs project_idea and github")
    for req in report.requirements:
        if req.status != "have" and req.id not in covered:
            errors.append(f"requirement '{req.id}' ({req.name}) is {req.status} but no recommendation closes it")
    if len(report.quick_wins) > 3:
        errors.append(f"quick_wins has {len(report.quick_wins)} items; max 3")
    if any(not w.strip() for w in report.quick_wins):
        errors.append("quick_wins has an empty item")
    return errors


def _norm_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def collect_result_urls(contents: list) -> set[str]:
    """Every `url` found in search results and citations (not in the submitted report)."""
    urls = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "url" and isinstance(value, str) and value.startswith(("http://", "https://")):
                    urls.add(_norm_url(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for content in contents:
        for block in content:
            data = block.model_dump() if hasattr(block, "model_dump") else block
            if isinstance(data, dict) and data.get("type") == "tool_use" and data.get("name") == TOOL_NAME:
                continue
            walk(data)
    return urls


def search_link(title: str, provider: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(f"{title} {provider}")


def resolve_links(report: dict, allowed: set[str]) -> tuple[dict, list[str]]:
    """Keep a recommendation's url only if it came from search results; add search_url otherwise."""
    report = copy.deepcopy(report)
    dropped = []
    for rec in report["recommendations"]:
        url = rec.get("url")
        if url and _norm_url(url) not in allowed:
            dropped.append(url)
            url = None
        rec["url"] = url
        rec["search_url"] = None if url else search_link(rec["title"], rec["provider"])
    return report, dropped


# ---------------------------
# API CALL
# ---------------------------
@dataclass
class GapResult:
    report: dict  # GapReport fields, with verified urls and search_url added
    usage: dict
    model: str
    repairs: list[dict]
    web_search: dict  # enabled, requests, urls_found, urls_dropped
    raw: dict = field(repr=False)


def _web_searches(message) -> int:
    server = getattr(message.usage, "server_tool_use", None)
    return int(getattr(server, "web_search_requests", 0) or 0)


def _repair_message(tool_use_id: str, errors: list[str]) -> dict:
    listing = "\n".join(f"- {e}" for e in errors)
    return {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
        "content": f"The report has {len(errors)} problem(s):\n{listing}\n\nFix every problem and call {TOOL_NAME} again "
                   "with the complete corrected report.",
    }]}


def gap_report(profile: dict, jd: str, *, client=None, model: str | None = None, effort: str | None = None,
               progress=None) -> GapResult:
    progress = progress or (lambda message: None)
    client = client or anthropic.Anthropic()
    model = model or os.environ.get("CV_MODEL") or DEFAULT_MODEL
    effort = effort or os.environ.get("CV_EFFORT") or DEFAULT_EFFORT
    web_search = _tailor.web_search_enabled()
    request = build_gap_request(profile, jd, model, effort, web_search)
    messages = list(request["messages"])
    contents, calls, repairs, searches, pauses = [], [], [], 0, 0
    progress("Gap report" + (" (with web search)" if web_search else ""))

    while True:
        with client.beta.messages.stream(**{**request, "messages": messages}) as stream:
            message = stream.get_final_message()
        usage = _usage(message)
        usage["web_search_requests"] = _web_searches(message)
        searches += usage["web_search_requests"]
        calls.append(usage)
        log.info("gap report %s (call %d): input=%d output=%d cache_read=%d searches=%d stop=%s", message.model,
                 len(calls), usage["input_tokens"], usage["output_tokens"], usage["cache_read_input_tokens"],
                 usage["web_search_requests"], message.stop_reason)
        if message.stop_reason == "refusal":
            raise TailorError("gap report refused")
        if message.stop_reason == "max_tokens":
            raise TailorError(f"gap report hit max_tokens={MAX_TOKENS}")
        contents.append(message.content)
        messages.append({"role": "assistant", "content": message.content})
        if message.stop_reason == "pause_turn":  # server-side search loop paused; resend to resume
            pauses += 1
            if pauses > MAX_PAUSES:
                raise TailorError("gap report: web search kept pausing")
            continue

        call = next((b for b in message.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        if call is None:
            raise TailorError(f"Claude didn't call {TOOL_NAME}")
        raw = copy.deepcopy(call.input)
        try:
            report = GapReport.model_validate(raw)
            errors = validate_report(report, profile)
        except ValidationError as exc:
            report, errors = None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]

        total = {k: sum(c[k] for c in calls) for k in calls[0]}
        if not errors:
            allowed = collect_result_urls(contents) if web_search else set()
            resolved, dropped = resolve_links(report.model_dump(), allowed)
            for url in dropped:
                log.warning("gap report: dropped a URL not found in search results: %s", url)
            return GapResult(report=resolved, usage={**total, "calls": calls}, model=message.model, repairs=repairs,
                             web_search={"enabled": web_search, "requests": searches, "urls_found": len(allowed),
                                         "urls_dropped": dropped}, raw=raw)
        repairs.append({"attempt": len(repairs) + 1, "errors": errors})
        if len(repairs) > MAX_REPAIRS:
            raise TailorError(f"gap report still invalid after {MAX_REPAIRS} repairs:\n" + "\n".join(f"  - {e}" for e in errors))
        progress(f"Repairing the gap report: {len(errors)} error(s) (attempt {len(repairs)}/{MAX_REPAIRS})")
        messages.append(_repair_message(call.id, errors))


# ---------------------------
# OUTPUT
# ---------------------------
def render_markdown(report: dict, company: str, role: str, day: date | None = None) -> str:
    fit, reqs = report["fit"], report["requirements"]
    by_id = {r["id"]: r for r in reqs}
    counts = {s: sum(1 for r in reqs if r["status"] == s) for s in ("have", "partial", "missing")}
    lines = [
        f"# Skill-gap report: {role} at {company}",
        "",
        f"*{(day or date.today()).isoformat()}*",
        "",
        f"## Fit: {fit['score']}/100",
        "",
        f"**{fit['justification']}**",
        "",
        fit["summary"],
        "",
        f"## Requirements ({counts['have']} have, {counts['partial']} partial, {counts['missing']} missing)",
        "",
        "| Requirement | Importance | Status | Evidence | Note |",
        "|---|---|---|---|---|",
    ]
    order = {"missing": 0, "partial": 1, "have": 2}
    for r in sorted(reqs, key=lambda r: (r["importance"] != "must-have", order[r["status"]])):
        cell = lambda s: str(s).replace("|", "\\|")
        evidence = ", ".join(f"`{e}`" for e in r["evidence"]) or "-"
        lines.append(f"| {cell(r['name'])} | {r['importance']} | **{r['status']}** | {evidence} | {cell(r['note'])} |")

    recs = report["recommendations"]
    link = lambda rec: f"[link]({rec['url']})" if rec.get("url") else f"[search]({rec['search_url']})"

    # By gap: one line per partial/missing requirement, naming the recommendations (by number) that close it.
    lines += ["", "## Gaps and what closes them", ""]
    for r in sorted((r for r in reqs if r["status"] != "have"),
                    key=lambda r: (r["importance"] != "must-have", order[r["status"]])):
        closing = [f"#{n} {rec['title']}" for n, rec in enumerate(recs, 1) if r["id"] in rec["requirement_ids"]]
        lines.append(f"- **{r['name']}** ({r['status']}, {r['importance']}): {'; '.join(closing) or 'no recommendation'}")

    # Details once per recommendation, biggest improvement to fit first.
    lines += ["", "## Recommendations (biggest impact first)", ""]
    for n, rec in enumerate(recs, 1):
        closes = ", ".join(by_id[i]["name"] for i in rec["requirement_ids"] if i in by_id)
        lines += [
            f"### {n}. {rec['type'].capitalize()}: {rec['title']}",
            "",
            f"{rec['provider']} · ~{rec['effort_hours']:g} h · {rec['cost']} · {link(rec)}",
            "",
            rec["why"],
            "",
            f"*Closes:* {closes}",
        ]
        if rec.get("project_idea"):
            lines += ["", f"*Build:* {rec['project_idea']}"]
        if rec.get("github"):
            lines += ["", f"*On GitHub:* {rec['github']}"]
        lines.append("")

    lines += ["## Quick wins (this week)", ""]
    lines += [f"{i}. {w}" for i, w in enumerate(report["quick_wins"], 1)] or ["None."]
    lines.append("")
    return "\n".join(lines)


def write_gap_report(folder: Path, result: GapResult, company: str, role: str) -> tuple[Path, Path]:
    folder = Path(folder)
    data = {"company": company, "role": role, "date": date.today().isoformat(), "model": result.model,
            **result.report, "web_search": result.web_search, "repairs": result.repairs, "usage": result.usage}
    json_path, md_path = folder / "gap_report.json", folder / "gap_report.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result.report, company, role), encoding="utf-8")
    return json_path, md_path


def update_backlog(output_root: Path) -> Path:
    """Rebuild output/skills_backlog.md from every gap_report.json under output_root."""
    output_root = Path(output_root)
    reports = sorted(output_root.glob("*/gap_report.json"))
    skills = {}
    for path in reports:
        data = json.loads(path.read_text(encoding="utf-8"))
        job = f"{data.get('company', '?')} ({path.parent.name})"
        for req in data.get("requirements", []):
            if req["status"] == "have":
                continue
            key = re.sub(r"\s+", " ", req["skill"].strip()).lower()
            entry = skills.setdefault(key, {"label": req["skill"].strip(), "missing": 0, "partial": 0, "must": 0, "jobs": []})
            entry[req["status"]] += 1
            entry["must"] += req["importance"] == "must-have"
            if job not in entry["jobs"]:
                entry["jobs"].append(job)
    rows = sorted(skills.values(), key=lambda e: (-(e["missing"] + e["partial"]), -e["must"], -e["missing"], e["label"].lower()))
    lines = [
        "# Skills backlog",
        "",
        f"Gaps across {len(reports)} job description(s), most frequent first. Rebuilt from every "
        "`gap_report.json` under `output/` after each run.",
        "",
        "| Skill | Missing | Partial | Must-have | Jobs |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {e['label']} | {e['missing']} | {e['partial']} | {e['must']} | {'; '.join(e['jobs'])} |" for e in rows]
    if not rows:
        lines.append("| (no gaps yet) | | | | |")
    path = output_root / "skills_backlog.md"
    output_root.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
