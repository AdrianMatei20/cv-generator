"""Ask the Claude API to select and rephrase profile content into a CV JSON for a given job description.

    result = tailor(profile_dict, jd_text)
    result.cv      # CV JSON for cvgen.render.render()
    result.meta()  # company, role, source ids, gaps, repairs, usage -> cv.meta.json

Claude answers through one strict tool, `submit_cv`, whose input schema comes from
TailoredCV below. Claude only chooses profile items by id and writes the text that
needs judgment (headline, summary, skill lines, bullets), each with the profile ids
it came from. Titles, companies, dates, locations, education, certificates and
contact details are copied from the profile by id, so they can't drift.

Each submission is checked with cvgen.validate.validate(). If it has errors, the
previous submission and the error list go back to Claude in the same conversation
and it submits again, up to MAX_REPAIRS times; after that tailor() raises
TailorValidationError, saving the last attempt for hand-editing if draft_dir is given.

The system prompt and the profile are sent as cached blocks; the JD goes in the
uncached user message. Token usage, including cache reads, is logged at INFO.
"""

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cvgen import costs, limits, settings
from cvgen.profile import check_profile
from cvgen.profile_schema import Certification, Education, Job, Profile, Project, format_month
from cvgen.schema import split_list
from cvgen.validate import validate

DEFAULT_MODEL = settings.DEFAULTS["TAILOR_MODEL"]  # override with TAILOR_MODEL (or CV_MODEL)
DEFAULT_EFFORT = settings.DEFAULTS["TAILOR_EFFORT"]  # override with TAILOR_EFFORT (or CV_EFFORT)
TOOL_NAME = "submit_cv"
MAX_TOKENS = 16000  # ceiling incl. thinking; the largest call seen used ~10.6k at effort high
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TOOL_CHANGES_BETA = "mid-conversation-tool-changes-2026-07-01"
# Same betas on every profile call (tailor, gap report), so they share one cache prefix.
BETAS = [FALLBACK_BETA, TOOL_CHANGES_BETA]
MAX_REPAIRS = 2

log = logging.getLogger(__name__)


class TailorError(RuntimeError):
    pass


# ---------------------------
# TOOL INPUT
# ---------------------------
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourcedText(_Model):
    text: str = Field(description="Plain text written for this CV.")
    source_ids: list[str] = Field(description="Ids of the profile items this text was built from.")


class SkillLine(_Model):
    category: str = Field(description="Category label shown on the CV, e.g. 'Databases'.")
    skills: str = Field(description="Comma-separated skills, most relevant to the JD first.")
    source_ids: list[str] = Field(description="Ids of the profile skills listed on this line.")


class JobPick(_Model):
    job_id: str = Field(description="Id of a profile experience entry.")
    bullets: list[SourcedText] = Field(description="Bullets for this job, most relevant first. Built only from this job's profile bullets.")


class ProjectPick(_Model):
    project_id: str = Field(description="Id of a profile project.")
    bullets: list[SourcedText] = Field(
        description="Bullets for this project, most relevant first. Built from its `bullets` or its `compact_bullets`, never both."
    )


class TailoredCV(_Model):
    company: str = Field(description="Hiring company's short name from the JD, e.g. 'Google'. 'Unknown' if the JD doesn't say.")
    role: str = Field(description="Role title from the JD, e.g. 'Software Engineer'.")
    headline: SourcedText
    summary: SourcedText
    skills: list[SkillLine]
    experience: list[JobPick] = Field(description="Every profile job, each with at least one bullet.")
    projects: list[ProjectPick]
    education_ids: list[str] = Field(description="Ids of profile education entries to show.")
    certification_ids: list[str] = Field(description="Ids of profile certifications to show, most relevant first.")
    gaps: list[str] = Field(description="JD requirements the profile has no evidence for. Left out of the CV.")


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema adapted for strict tool use: every object closed and fully required,
    and no keywords strict mode rejects. Length limits are checked after parsing instead."""
    schema = model.model_json_schema()

    def clean(node, is_names=False):
        if isinstance(node, dict):
            if not is_names:
                for key in ("title", "default", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
                    node.pop(key, None)
                if node.get("type") == "object" and "properties" in node:
                    node["additionalProperties"] = False
                    node["required"] = list(node["properties"])
            for key, value in node.items():
                clean(value, is_names=not is_names and key in ("properties", "$defs"))
        elif isinstance(node, list):
            for value in node:
                clean(value)

    clean(schema)
    return schema


TOOL = {
    "name": TOOL_NAME,
    "description": "Submit the tailored CV. Call this exactly once, with the complete CV.",
    "strict": True,
    "input_schema": strict_schema(TailoredCV),
}


# ---------------------------
# PROMPT
# ---------------------------
# Shared by every call that works from the profile (tailoring, gap report). It goes before
# the profile, so it's part of the cached prefix: keep it free of per-run or per-task text.
SHARED_PROMPT = """\
You work for a job candidate. Their master profile is in <profile> below; it is the only source of facts about them. Each request brings a job description (JD) in the user message, and the task to do with it comes after the profile.

How the profile works
- Every item has an id. Jobs and projects hold bullets. A bullet's `text` is its base wording, and its optional `facts` are extra details (numbers, technologies, scope) taken from other wordings of the same achievement. You may use a bullet's text and its facts, and nothing else.
- `angles` say what a bullet demonstrates; use them to judge relevance.
- A project may have `compact_bullets`, a shorter alternative to its `bullets` where one bullet can summarise several.
- `headlines` and `summaries` are variants labelled with a `target_role`.
- Jobs with `positions` (e.g. internships) are shown as one entry; a bullet's `position` says which position it belongs to.
- `preferences` is written by the candidate. Let `target_roles` and `tone` guide your choices, and never write anything listed in `never_claim`. Preferences are not facts.

Never invent
- Never claim or imply any skill, tool, technology, employer, job title, date, duration, number, metric or certificate that the profile doesn't support. Don't round, inflate or combine numbers into new ones.
"""


def system_prompt() -> str:
    """The tailoring task, after the cached profile. Built per request from cvgen.limits, so a
    runtime override (e.g. --pages) applies without touching the cached prefix."""
    return f"""\
Task: tailor the candidate's CV to the JD. Submit the result by calling the {TOOL_NAME} tool exactly once; don't answer in plain text.

Choosing content
- Pick the experience, projects and skills most relevant to this JD; shorten or drop the rest. Keep every job so the work history has no gaps, but give older or unrelated jobs just one or two bullets.
- Order bullets within each job and project by relevance to the JD, most relevant first. Choose the projects that best support the JD.
- For a project with `compact_bullets`, use either its `bullets` or its `compact_bullets`, never both.
- Start the headline and summary from the closest `target_role` variant and adapt them to the JD.
- Use the JD's own words for skills, technologies and activities wherever they are true for the candidate. For example, if the JD says "root cause analysis" and a bullet describes tracing a production issue to its cause, you may call it root cause analysis. Never use a JD term to claim something the profile doesn't support.

Sources
- If the JD asks for something the profile lacks, leave it out of the CV and list it in `gaps`.
- Job titles, companies, dates, locations, education and certificate details are filled in from the profile by id. You only choose which appear and in what order.
- Every piece of text you write lists the ids of the profile items it came from in `source_ids`. A bullet built from two profile bullets lists both; a skills line lists the ids of its skills.

Writing
- Start each bullet with a strong action verb, lead with the result, and keep the numbers the profile gives.
- No first person (I, my, we, our). Past tense for jobs that have ended; present tense for a current job (`end` is null).
- Plain text only: no markdown, and never the characters < or >.

Length: the rendered CV must fit on {limits.MAX_PAGES} pages
- Headline: at most {limits.MAX_HEADLINE_CHARS} characters (one line).
- Summary: at most {limits.MAX_SUMMARY_CHARS} characters.
- Bullets: at most {limits.MAX_BULLET_CHARS} characters, i.e. two lines of about {limits.CHARS_PER_LINE} characters. Avoid {limits.CHARS_PER_LINE + 5}-{limits.CHARS_PER_LINE + 25} characters: that wraps onto a second line holding only a word or two. Stay under {limits.CHARS_PER_LINE} or use most of the second line.
- At most {limits.MAX_BULLETS_PER_ROLE} bullets per job, {limits.MAX_BULLETS_PER_PROJECT} per project, and {limits.MAX_PROJECTS} projects.
- Skills: at most {limits.MAX_SKILL_CATEGORIES} categories and {limits.MAX_SKILLS_TOTAL} skills in total. Each category line (name plus its comma-separated skills) is at most {limits.MAX_SKILL_LINE_CHARS} characters. Put the skills the JD cares about most first.
"""


# Fields Claude never needs: traceability (`sources`), and what Python copies in itself
# (contact details, project links).
UNUSED_FIELDS = {"sources", "link"}


def _strip_unused(node):
    if isinstance(node, dict):
        return {k: _strip_unused(v) for k, v in node.items() if k not in UNUSED_FIELDS}
    if isinstance(node, list):
        return [_strip_unused(v) for v in node]
    return node


def profile_block(profile: dict) -> str:
    """The profile as Claude sees it: minified, keys sorted (byte-stable, so the cached prefix hits),
    without contact details, links or sources."""
    data = _strip_unused({k: v for k, v in profile.items() if k != "contact"})
    return "<profile>\n" + json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n</profile>"


def web_search_enabled() -> bool:
    return settings.get("GAP_WEB_SEARCH").lower() not in ("0", "false", "no", "off")


def shared_tools() -> list[dict]:
    """The tool list every profile call sends, byte-identical, so they share one cache prefix.

    submit_cv is live; submit_gap_report and web_search are declared deferred and only
    surfaced (with tool_addition) by the gap-report call.
    """
    from cvgen.gaps import GAP_TOOL, web_search_tool  # deferred import: cvgen.gaps imports this module

    return [TOOL, GAP_TOOL] + ([web_search_tool()] if web_search_enabled() else [])


def shared_system(profile: dict, task_prompt: str) -> list[dict]:
    """Shared rules + profile (cached), then the task (not cached)."""
    return [
        {"type": "text", "text": SHARED_PROMPT},
        {"type": "text", "text": profile_block(profile), "cache_control": settings.cache_control()},
        {"type": "text", "text": task_prompt},
    ]


def build_request(profile: dict, jd: str, model: str, effort: str) -> dict:
    """Request kwargs. Tools, shared rules and profile are byte-stable across runs and tasks, so they cache."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "betas": BETAS,
        "fallbacks": "default",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "tools": shared_tools(),
        # Opus 5.5 rejects a forced tool_choice; strict + the prompt make the call reliable,
        # and tailor() checks it happened.
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "system": shared_system(profile, system_prompt()),
        "messages": [{
            "role": "user",
            "content": f"<job_description>\n{jd.strip()}\n</job_description>\n\nTailor the CV to this job and submit it with {TOOL_NAME}.",
        }],
    }


# ---------------------------
# RESULT
# ---------------------------
@dataclass
class TailorResult:
    cv: dict  # CV JSON for render()
    company: str
    role: str
    sources: dict  # same shape as the CV, with profile ids in place of text
    gaps: list[str]
    problems: list[str]  # validation errors left in this CV; empty for a successful tailor()
    repairs: list[dict]  # one entry per rejected submission: {"attempt": n, "errors": [...]}
    usage: dict  # token counts summed over all calls, plus "calls": one cvgen.costs row per call
    model: str
    raw: dict = field(repr=False)  # the last submit_cv tool input as returned
    python_fixes: list[str] = field(default_factory=list)  # removals python_fixes() made instead of a repair call

    def meta(self) -> dict:
        """Everything for cv.meta.json: traceability and diagnostics, never rendered."""
        return {
            "company": self.company, "role": self.role, "model": self.model, "sources": self.sources,
            "gaps": self.gaps, "problems": self.problems, "repairs": self.repairs, "python_fixes": self.python_fixes,
            "usage": self.usage,
        }


class TailorValidationError(TailorError):
    """The CV still had errors after MAX_REPAIRS repair attempts."""

    def __init__(self, result: TailorResult, saved_to: list[Path]):
        self.result = result
        self.saved_to = saved_to
        lines = [f"CV still invalid after {MAX_REPAIRS} repair attempts ({len(result.problems)} error(s)):"]
        lines += [f"  - {p}" for p in result.problems]
        if saved_to:
            lines.append("Last attempt saved for hand-editing: " + ", ".join(str(p) for p in saved_to))
        super().__init__("\n".join(lines))


def _usage(message) -> dict:
    u = message.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
        "cache_read_input_tokens": u.cache_read_input_tokens or 0,
    }


def assemble(tailored: TailoredCV, profile: Profile) -> tuple[dict, dict, list[str]]:
    """Turn Claude's selections into the render CV and its source map.

    Returns problems only for selections that can't appear in the CV at all (an id that
    isn't a job, project, education or certificate). Everything else is validate()'s job.
    """
    index = profile.index()
    problems = []

    def lookup(item_id, kind, where):
        item = index.get(item_id)
        if not isinstance(item, kind):
            problems.append(f"{where}: '{item_id}' is not a profile {kind.__name__.lower()}, so it was left out")
            return None
        return item

    c = profile.contact
    cv = {"name": c.name, "headline": tailored.headline.text, "phone": c.phone, "email": c.email}
    if c.linkedin:
        cv.update(linkedinText=c.linkedin.text, linkedinUrl=c.linkedin.url)
    if c.github:
        cv.update(githubText=c.github.text, githubUrl=c.github.url)
    cv["summary"] = tailored.summary.text
    sources = {"headline": tailored.headline.source_ids, "summary": tailored.summary.source_ids}

    cv["skills"] = [{"category": line.category, "skills": line.skills} for line in tailored.skills]
    sources["skills"] = [line.source_ids for line in tailored.skills]

    jobs = []
    for i, pick in enumerate(tailored.experience):
        job = lookup(pick.job_id, Job, f"experience job_id #{i + 1}")
        if job is not None:
            jobs.append((job, pick))
    jobs.sort(key=lambda jp: jp[0].start, reverse=True)  # reverse-chronological, whatever order came back
    cv["experience"] = [
        {"title": job.title, "company": job.company, "location": job.location, "period": job.period,
         "bullets": [b.text for b in pick.bullets]}
        for job, pick in jobs
    ]
    sources["experience"] = [{"id": job.id, "bullets": [b.source_ids for b in pick.bullets]} for job, pick in jobs]

    cv["projects"] = []
    sources["projects"] = []
    for i, pick in enumerate(tailored.projects):
        project = lookup(pick.project_id, Project, f"project_id #{i + 1}")
        if project is None:
            continue
        entry = {"name": project.name, "description": project.description}
        if project.link:
            entry.update(linkText=project.link.text, linkUrl=project.link.url)
        entry["bullets"] = [b.text for b in pick.bullets]
        cv["projects"].append(entry)
        sources["projects"].append({"id": project.id, "bullets": [b.source_ids for b in pick.bullets]})

    cv["education"] = []
    sources["education"] = []
    for i, item_id in enumerate(tailored.education_ids):
        edu = lookup(item_id, Education, f"education_ids #{i + 1}")
        if edu:
            cv["education"].append({"institution": edu.institution, "degree": edu.degree, "graduated": format_month(edu.graduated)})
            sources["education"].append(item_id)

    cv["certificates"] = []
    sources["certificates"] = []
    for i, item_id in enumerate(tailored.certification_ids):
        cert = lookup(item_id, Certification, f"certification_ids #{i + 1}")
        if cert:
            entry = {"certificate": cert.name, "date": format_month(cert.date) if cert.date else (cert.status or "")}
            if cert.details:
                entry["details"] = cert.details
            cv["certificates"].append(entry)
            sources["certificates"].append(item_id)
    return cv, sources, problems


def python_fixes(cv: dict) -> list[str]:
    """Fix in place, without a repair call, too many skills: the largest category (later on ties)
    loses its last skill until the total fits. Nothing is ever added or reworded. Text over a length
    limit (headline, summary, bullets) goes back to Claude instead, so no content is cut blindly."""
    fixes = []
    lines = [[s.strip() for s in split_list(line["skills"])] for line in cv.get("skills", [])]
    while sum(map(len, lines)) > limits.MAX_SKILLS_TOTAL:
        size, i = max((len(names), i) for i, names in enumerate(lines))
        if size < 2:
            break
        fixes.append(f"skills ({cv['skills'][i]['category']}): removed '{lines[i].pop()}' to fit {limits.MAX_SKILLS_TOTAL} skills")
        cv["skills"][i]["skills"] = ", ".join(lines[i])
    return fixes


def _repair_message(tool_use_id: str, errors: list[str]) -> dict:
    listing = "\n".join(f"- {e}" for e in errors)
    return {"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": True,
        "content": (
            f"The CV you submitted has {len(errors)} problem(s):\n{listing}\n\n"
            "Paths refer to the rendered CV: experience is ordered newest first, and titles, companies, dates, "
            "education and certificates are filled in from the profile by id. Fix every problem without breaking "
            f"the other rules, then call {TOOL_NAME} again with the complete corrected CV."
        ),
    }]}


def _save_draft(draft_dir: Path, cv: dict, meta: dict) -> list[Path]:
    draft_dir.mkdir(parents=True, exist_ok=True)
    paths = [draft_dir / "cv.draft.json", draft_dir / "cv.draft.meta.json"]
    for path, data in zip(paths, (cv, meta)):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths


# ---------------------------
# API CALL
# ---------------------------
def tailor(profile: dict, jd: str, *, client: anthropic.Anthropic | None = None, model: str | None = None,
           effort: str | None = None, draft_dir: Path | None = None, progress=None) -> TailorResult:
    """`progress`, if given, is called with a short message at each stage (for the CLI)."""
    progress = progress or (lambda message: None)
    checked = check_profile(profile)
    if checked.errors:
        raise TailorError("profile has errors:\n" + "\n".join(checked.errors))
    client = client or anthropic.Anthropic()
    model = model or settings.tailor_model()
    effort = effort or settings.tailor_effort()
    request = build_request(profile, jd, model, effort)
    messages = list(request["messages"])
    usages, calls, repairs = [], [], []  # usages: raw token counts for totals; calls: one cost row per call
    nudged = False  # tool_choice is "auto": allow one reminder if Claude answers without the tool
    progress(f"Tailoring with {model} (effort {effort})")

    while True:
        with client.beta.messages.stream(**{**request, "messages": messages}) as stream:
            message = stream.get_final_message()
        usage = _usage(message)
        usages.append(usage)
        calls.append(costs.call_row("repair" if repairs else "tailor", message, effort))
        log.info(
            "tailor %s (call %d): input=%d output=%d cache_write=%d cache_read=%d stop=%s",
            message.model, len(calls), usage["input_tokens"], usage["output_tokens"],
            usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"], message.stop_reason,
        )
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise TailorError(f"request refused ({getattr(details, 'category', None)}): {getattr(details, 'explanation', '')}")
        if message.stop_reason == "max_tokens":
            raise TailorError(f"hit max_tokens={MAX_TOKENS} before finishing")
        # Pass the assistant turn back unchanged (thinking blocks included) on any follow-up.
        messages.append({"role": "assistant", "content": message.content})

        call = next((b for b in message.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        if call is None:
            if nudged:
                text = " ".join(b.text for b in message.content if b.type == "text")
                raise TailorError(f"Claude didn't call {TOOL_NAME}: {text[:500]}")
            nudged = True
            messages.append({"role": "user", "content": f"Submit the CV by calling {TOOL_NAME}."})
            continue

        raw = copy.deepcopy(call.input)
        progress("Validating")
        try:
            tailored = TailoredCV.model_validate(raw)
        except ValidationError as exc:
            errors = [f"submit_cv input {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
            cv, sources, company, role, gaps, fixes = {}, {}, "", "", [], []
        else:
            cv, sources, errors = assemble(tailored, checked.profile)
            company, role, gaps = tailored.company, tailored.role, tailored.gaps
            fixes = python_fixes(cv)
            for fix in fixes:
                log.info("tailor: fixed in Python: %s", fix)
            errors += validate(cv, {"sources": sources}, profile)

        total = {k: sum(u[k] for u in usages) for k in usages[0]}
        result = TailorResult(cv=cv, company=company, role=role, sources=sources, gaps=gaps, problems=errors,
                              repairs=repairs, usage={**total, "calls": calls}, model=message.model, raw=raw,
                              python_fixes=fixes)
        if not errors:
            return result
        for error in errors:
            log.warning("tailor: attempt %d: %s", len(repairs) + 1, error)
        repairs.append({"attempt": len(repairs) + 1, "errors": errors})
        if len(repairs) > MAX_REPAIRS:
            saved = _save_draft(Path(draft_dir), cv, result.meta()) if draft_dir else []
            raise TailorValidationError(result, saved)
        log.info("tailor: asking Claude to fix %d error(s) (repair %d of %d)", len(errors), len(repairs), MAX_REPAIRS)
        progress(f"Repairing {len(errors)} validation error(s) (attempt {len(repairs)}/{MAX_REPAIRS})")
        messages.append(_repair_message(call.id, errors))
