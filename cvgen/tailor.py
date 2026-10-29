"""Ask the Claude API to select and rephrase profile content into a CV JSON for a given job description.

    result = tailor(profile_dict, jd_text)
    result.cv      # CV JSON for cvgen.render.render()
    result.meta()  # company, role, source ids, gaps, problems, usage -> cv.meta.json

Claude answers through one strict tool, `submit_cv`, whose input schema comes from
TailoredCV below. Claude only chooses profile items by id and writes the text that
needs judgment (headline, summary, skill lines, bullets), each with the profile ids
it came from. Titles, companies, dates, locations, education, certificates and
contact details are copied from the profile by id, so they can't drift.

The system prompt and the profile are sent as cached blocks; the JD goes in the
uncached user message. Token usage, including cache reads, is logged at INFO.
"""

import copy
import json
import logging
import os
from dataclasses import dataclass, field

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cvgen import limits
from cvgen.profile import check_profile
from cvgen.profile_schema import Certification, Education, Job, Profile, Project, format_month
from cvgen.schema import CV

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
TOOL_NAME = "submit_cv"
MAX_TOKENS = 32000
FALLBACK_BETA = "server-side-fallback-2026-07-01"

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
SYSTEM_PROMPT = f"""\
You tailor a candidate's CV to a job description (JD). The candidate's master profile is in <profile> below; it is the only source of facts about them. The JD arrives in the user message. Submit the result by calling the {TOOL_NAME} tool exactly once; don't answer in plain text.

How the profile works
- Every item has an id. Jobs and projects hold bullets. A bullet's `text` is its base wording, and its optional `facts` are extra details (numbers, technologies, scope) taken from other wordings of the same achievement. You may use a bullet's text and its facts, and nothing else.
- `angles` say what a bullet demonstrates; use them to judge relevance.
- A project may have `compact_bullets`, a shorter alternative to its `bullets` where one bullet can summarise several. Use one set or the other for a project, never both.
- `headlines` and `summaries` are variants labelled with a `target_role`. Start from the closest one and adapt it to the JD.
- Jobs with `positions` (e.g. internships) are shown as one entry; a bullet's `position` says which position it belongs to.
- `preferences` is written by the candidate. Let `target_roles` and `tone` guide your choices, and never write anything listed in `never_claim`. Preferences are not facts.

Choosing content
- Pick the experience, projects and skills most relevant to this JD; shorten or drop the rest. Keep every job so the work history has no gaps, but give older or unrelated jobs just one or two bullets.
- Order bullets within each job and project by relevance to the JD, most relevant first. Choose the projects that best support the JD.
- Use the JD's own words for skills, technologies and activities wherever they are true for the candidate. For example, if the JD says "root cause analysis" and a bullet describes tracing a production issue to its cause, you may call it root cause analysis. Never use a JD term to claim something the profile doesn't support.

Never invent
- Don't add any skill, tool, technology, employer, job title, date, duration, number, metric or certificate that isn't in the profile. Don't round, inflate or combine numbers into new ones.
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


def _strip_sources(node):
    if isinstance(node, dict):
        return {k: _strip_sources(v) for k, v in node.items() if k != "sources"}
    if isinstance(node, list):
        return [_strip_sources(v) for v in node]
    return node


def profile_block(profile: dict) -> str:
    """The profile as Claude sees it: no `sources` (traceability only), stable serialisation."""
    return "<profile>\n" + json.dumps(_strip_sources(profile), ensure_ascii=False, indent=1) + "\n</profile>"


def build_request(profile: dict, jd: str, model: str, effort: str) -> dict:
    """Request kwargs. Everything before the JD is byte-stable across runs, so it caches."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "tools": [TOOL],
        # Opus 5.5 rejects a forced tool_choice; strict + the prompt make the call reliable,
        # and tailor() checks it happened.
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "system": [
            {"type": "text", "text": SYSTEM_PROMPT},
            {"type": "text", "text": profile_block(profile), "cache_control": {"type": "ephemeral"}},
        ],
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
    cv: dict  # CV JSON for render(); validated against cvgen.schema.CV unless `problems` says otherwise
    company: str
    role: str
    sources: dict  # same shape as the CV, with profile ids in place of text
    gaps: list[str]
    problems: list[str]  # unknown/misplaced ids and schema or length violations; empty when clean
    usage: dict
    model: str
    raw: dict = field(repr=False)  # the submit_cv tool input as returned

    def meta(self) -> dict:
        """Everything for cv.meta.json: traceability and diagnostics, never rendered."""
        return {
            "company": self.company, "role": self.role, "model": self.model,
            "sources": self.sources, "gaps": self.gaps, "problems": self.problems, "usage": self.usage,
        }


def _usage(message) -> dict:
    u = message.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
        "cache_read_input_tokens": u.cache_read_input_tokens or 0,
    }


def assemble(tailored: TailoredCV, profile: Profile) -> tuple[dict, dict, list[str]]:
    """Turn Claude's selections into the render CV, its source map, and any problems found."""
    index = profile.index()
    problems = []

    def check(ids, where, allowed=None):
        for item_id in ids:
            if item_id not in index:
                problems.append(f"{where}: unknown profile id '{item_id}'")
            elif allowed is not None and item_id not in allowed:
                problems.append(f"{where}: id '{item_id}' doesn't belong here")
        if not ids:
            problems.append(f"{where}: no source_ids")

    def lookup(item_id, kind, where):
        item = index.get(item_id)
        if not isinstance(item, kind):
            problems.append(f"{where}: '{item_id}' is not a profile {kind.__name__.lower()}")
            return None
        return item

    c = profile.contact
    cv = {"name": c.name, "headline": tailored.headline.text, "phone": c.phone, "email": c.email}
    if c.linkedin:
        cv.update(linkedinText=c.linkedin.text, linkedinUrl=c.linkedin.url)
    if c.github:
        cv.update(githubText=c.github.text, githubUrl=c.github.url)
    cv["summary"] = tailored.summary.text
    check(tailored.headline.source_ids, "headline")
    check(tailored.summary.source_ids, "summary")
    sources = {"headline": tailored.headline.source_ids, "summary": tailored.summary.source_ids}

    cv["skills"] = []
    sources["skills"] = []
    for i, line in enumerate(tailored.skills):
        check(line.source_ids, f"skills[{i}]")
        cv["skills"].append({"category": line.category, "skills": line.skills})
        sources["skills"].append(line.source_ids)

    jobs = []
    for i, pick in enumerate(tailored.experience):
        job = lookup(pick.job_id, Job, f"experience[{i}]")
        if job is None:
            continue
        own = {b.id for b in job.bullets}
        for j, b in enumerate(pick.bullets):
            check(b.source_ids, f"experience[{i}].bullets[{j}]", own)
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
        project = lookup(pick.project_id, Project, f"projects[{i}]")
        if project is None:
            continue
        regular = {b.id for b in project.bullets}
        compact = {b.id for b in project.compact_bullets}
        used = set()
        for j, b in enumerate(pick.bullets):
            check(b.source_ids, f"projects[{i}].bullets[{j}]", regular | compact)
            used.update(b.source_ids)
        if used & regular and used & compact:
            problems.append(f"projects[{i}]: mixes bullets and compact_bullets of '{project.id}'")
        entry = {"name": project.name, "description": project.description}
        if project.link:
            entry.update(linkText=project.link.text, linkUrl=project.link.url)
        entry["bullets"] = [b.text for b in pick.bullets]
        cv["projects"].append(entry)
        sources["projects"].append({"id": project.id, "bullets": [b.source_ids for b in pick.bullets]})

    cv["education"] = []
    for i, item_id in enumerate(tailored.education_ids):
        edu = lookup(item_id, Education, f"education[{i}]")
        if edu:
            cv["education"].append({"institution": edu.institution, "degree": edu.degree, "graduated": format_month(edu.graduated)})
    sources["education"] = [i for i in tailored.education_ids if isinstance(index.get(i), Education)]

    cv["certificates"] = []
    for i, item_id in enumerate(tailored.certification_ids):
        cert = lookup(item_id, Certification, f"certificates[{i}]")
        if cert:
            entry = {"certificate": cert.name, "date": format_month(cert.date) if cert.date else (cert.status or "")}
            if cert.details:
                entry["details"] = cert.details
            cv["certificates"].append(entry)
    sources["certificates"] = [i for i in tailored.certification_ids if isinstance(index.get(i), Certification)]

    never = [n.lower() for n in profile.preferences.never_claim]
    text = json.dumps(cv, ensure_ascii=False).lower()
    problems += [f"CV contains '{n}', which preferences.never_claim forbids" for n in never if n and n in text]

    try:
        cv = CV.model_validate(cv).to_render_dict()
    except ValidationError as exc:
        for err in exc.errors():
            problems.append(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}")
    return cv, sources, problems


# ---------------------------
# API CALL
# ---------------------------
def tailor(profile: dict, jd: str, *, client: anthropic.Anthropic | None = None,
           model: str | None = None, effort: str | None = None) -> TailorResult:
    checked = check_profile(profile)
    if checked.errors:
        raise TailorError("profile has errors:\n" + "\n".join(checked.errors))
    client = client or anthropic.Anthropic()
    model = model or os.environ.get("CV_MODEL") or DEFAULT_MODEL
    effort = effort or os.environ.get("CV_EFFORT") or DEFAULT_EFFORT
    request = build_request(profile, jd, model, effort)

    for attempt in (1, 2):  # tool_choice is "auto", so retry once if Claude answers without the tool
        with client.beta.messages.stream(**request) as stream:
            message = stream.get_final_message()
        usage = _usage(message)
        log.info(
            "tailor %s (attempt %d): input=%d output=%d cache_write=%d cache_read=%d stop=%s",
            message.model, attempt, usage["input_tokens"], usage["output_tokens"],
            usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"], message.stop_reason,
        )
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise TailorError(f"request refused ({getattr(details, 'category', None)}): {getattr(details, 'explanation', '')}")
        if message.stop_reason == "max_tokens":
            raise TailorError(f"hit max_tokens={MAX_TOKENS} before finishing")
        call = next((b for b in message.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        if call is not None:
            break
    else:
        text = " ".join(b.text for b in message.content if b.type == "text")
        raise TailorError(f"Claude didn't call {TOOL_NAME}: {text[:500]}")

    raw = copy.deepcopy(call.input)
    tailored = TailoredCV.model_validate(raw)
    cv, sources, problems = assemble(tailored, checked.profile)
    for problem in problems:
        log.warning("tailor: %s", problem)
    return TailorResult(cv=cv, company=tailored.company, role=tailored.role, sources=sources, gaps=tailored.gaps,
                        problems=problems, usage=usage, model=message.model, raw=raw)
