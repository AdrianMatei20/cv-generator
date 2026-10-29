"""Check the rendered PDF's layout and fix it with Claude.

    report = check_layout(pdf_path, cv)        # widows, page count, unmapped bullets
    cv, sources, meta = fix_layout(cv, sources, profile, jd, folder, basename)

Detection reads the PDF with PyMuPDF, groups spans into lines by baseline, and maps each
bullet (and the summary) to the lines it occupies by matching its text in document order
with whitespace removed, so line breaks, extra spaces, ligatures, bullet glyphs and
hyphenation at line ends don't matter. A wrapped bullet whose last line holds
WIDOW_MAX_WORDS words or fewer is a widow; a PDF longer than MAX_PAGES is over the limit.

The fix loop sends only the flagged bullets (with their source profile bullets) to Claude
through a small strict tool, `rewrite_bullets`, applies the rewrites and removals, runs
validate(), re-renders and re-checks, for up to MAX_LAYOUT_ROUNDS rounds. The best version
seen (within the page limit first, then fewest widows, then fewest pages) is kept.
"""

import copy
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import pymupdf
from pydantic import BaseModel, ConfigDict, Field

from cvgen import limits
from cvgen.profile_schema import Profile
from cvgen.render import render
from cvgen.tailor import DEFAULT_MODEL, FALLBACK_BETA, TailorError, _usage, strict_schema
from cvgen.validate import validate

LAYOUT_EFFORT = "medium"
TOOL_NAME = "rewrite_bullets"
MAX_TOKENS = 16000
_GLYPHS = "●•▪◦·‣∙○■□➢➤►-–—*"
_PATH = re.compile(r"^(experience|projects)\[(\d+)\]\.bullets\[(\d+)\]$")

log = logging.getLogger(__name__)


# ---------------------------
# DETECTION
# ---------------------------
@dataclass
class Line:
    page: int
    y: float  # baseline
    text: str


@dataclass
class Placement:
    path: str  # "summary" or "experience[0].bullets[2]"
    lines: list[Line]

    @property
    def last_line_words(self) -> int:
        return len(_strip_glyph(self.lines[-1].text).split())

    @property
    def chars_per_line(self) -> int:
        full = [len(_strip_glyph(l.text)) for l in self.lines[:-1]]
        return round(sum(full) / len(full)) if full else limits.CHARS_PER_LINE


@dataclass
class LayoutReport:
    pages: int
    placements: dict[str, Placement]
    widows: list[Placement]
    unmapped: list[str]
    lines_over: int  # text lines on pages beyond MAX_PAGES

    @property
    def over_pages(self) -> bool:
        return self.pages > limits.MAX_PAGES

    @property
    def ok(self) -> bool:
        return not self.widows and not self.over_pages

    def score(self) -> tuple:
        return (self.over_pages, len(self.widows), self.pages)

    def summary(self) -> dict:
        return {
            "pages": self.pages, "over_pages": self.over_pages, "lines_over": self.lines_over,
            "widows": [{"path": w.path, "lines": len(w.lines), "last_line_words": w.last_line_words,
                        "last_line": _strip_glyph(w.lines[-1].text)} for w in self.widows],
            "unmapped": self.unmapped,
        }


def _strip_glyph(text: str) -> str:
    return text.strip().lstrip(_GLYPHS).strip()


def _compact(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("­", "")  # NFKC expands ligatures like "ﬁ"
    return re.sub(r"\s+", "", text).lower()


def extract_lines(pdf_path: Path) -> tuple[int, list[Line]]:
    """All text lines in reading order, spans grouped by baseline."""
    lines = []
    with pymupdf.open(str(pdf_path)) as doc:
        pages = len(doc)
        for page_no, page in enumerate(doc):
            spans = [(s["origin"][1], s["bbox"][0], s["bbox"][2], s["text"])
                     for block in page.get_text("dict")["blocks"]
                     for line in block.get("lines", []) for s in line["spans"] if s["text"].strip()]
            spans.sort(key=lambda s: (s[0], s[1]))
            groups = []
            for span in spans:
                if groups and abs(span[0] - groups[-1][0][0]) <= 2.0:
                    groups[-1].append(span)
                else:
                    groups.append([span])
            for group in groups:
                group.sort(key=lambda s: s[1])
                text = group[0][3]
                for prev, span in zip(group, group[1:]):
                    text += ("" if span[1] - prev[2] < 1.0 else " ") + span[3]
                lines.append(Line(page_no, group[0][0], text))
    return pages, lines


def _match_from(target: str, lines: list[Line], start: int) -> int | None:
    """Number of lines from `start` that spell out `target` (compacted), or None."""
    pos, i = 0, start
    while pos < len(target):
        if i >= len(lines):
            return None
        chunk = _compact(_strip_glyph(lines[i].text) if i == start else lines[i].text)
        if not chunk or (i == start and len(chunk) < min(len(target), 8)):
            return None
        if target.startswith(chunk, pos):
            pos += len(chunk)
        elif chunk[-1] in "-‐" and target.startswith(chunk[:-1], pos):  # hyphenated at the line break
            pos += len(chunk) - 1
        else:
            return None
        i += 1
    return i - start


def _items(cv: dict) -> list[tuple[str, str]]:
    """(path, text) for the summary and every bullet, in document order."""
    items = [("summary", cv["summary"])] if cv.get("summary") else []
    for section in ("experience", "projects"):
        for i, entry in enumerate(cv.get(section) or []):
            items += [(f"{section}[{i}].bullets[{j}]", b) for j, b in enumerate(entry.get("bullets") or [])]
    return items


def check_layout(pdf_path: Path, cv: dict, widow_max_words: int = limits.WIDOW_MAX_WORDS) -> LayoutReport:
    pages, lines = extract_lines(pdf_path)
    placements, unmapped, cursor = {}, [], 0
    for path, text in _items(cv):
        target = _compact(text)
        for start in range(cursor, len(lines)):
            count = _match_from(target, lines, start)
            if count:
                placements[path] = Placement(path, lines[start:start + count])
                cursor = start + count
                break
        else:
            unmapped.append(path)
            log.warning("layout: couldn't find %s in the PDF: %.60s", path, text)
    widows = [p for p in placements.values() if len(p.lines) > 1 and p.last_line_words <= widow_max_words]
    lines_over = sum(1 for line in lines if line.page >= limits.MAX_PAGES)
    return LayoutReport(pages, placements, widows, unmapped, lines_over)


# ---------------------------
# FIX: CLAUDE CALL
# ---------------------------
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rewrite(_Model):
    path: str = Field(description="Path of a widow exactly as given, e.g. 'experience[0].bullets[2]' or 'summary'.")
    text: str = Field(description="The new text.")


class RewritePlan(_Model):
    rewrites: list[Rewrite] = Field(description="One entry per widow you rewrite.")
    remove: list[str] = Field(description="Paths of bullets to delete to get back within the page limit; empty if not over.")


TOOL = {
    "name": TOOL_NAME,
    "description": "Submit rewritten widow bullets and, if the CV is over the page limit, bullets to remove. Call exactly once.",
    "strict": True,
    "input_schema": strict_schema(RewritePlan),
}

def system_prompt() -> str:
    return f"""\
You fix layout problems in a rendered CV. Call the {TOOL_NAME} tool exactly once; don't answer in plain text.

Widows are bullets (or the summary) whose last line holds only a few words. For each widow, either shorten it so it fits on one line fewer, or extend it with true detail from its source bullets so the last line is fuller. Keep the meaning and the JD keywords it contains. A widow's entry tells you how many characters fit on a line; aim to end the last line at least half full, or remove the wrapped line entirely.

Use only facts from each widow's source bullets: no new numbers, tools, technologies or claims, and don't round or combine numbers. Start with a strong action verb, no first person (I, my, we, our), plain text without markdown or the characters < or >, and at most {limits.MAX_BULLET_CHARS} characters per bullet ({limits.MAX_SUMMARY_CHARS} for the summary).

If the CV is over the page limit, also list in `remove` the paths of the bullets least relevant to the JD, enough to free about the number of lines you're told it's over. Prefer bullets from older roles and weaker projects. Never remove a job's or a project's only bullet, and never remove the summary.
"""


def _source_lines(ids, index) -> list[str]:
    out = []
    for item_id in ids or []:
        item = index.get(item_id)
        if item is None:
            continue
        text = getattr(item, "text", None) or getattr(item, "name", "")
        facts = getattr(item, "facts", None)
        out.append(f"[{item_id}] {text}" + (f" | facts: {'; '.join(facts)}" if facts else ""))
    return out


def _bullet_sources(sources: dict, path: str):
    if path == "summary":
        return sources.get("summary")
    m = _PATH.match(path)
    try:
        return sources[m[1]][int(m[2])]["bullets"][int(m[3])]
    except (KeyError, IndexError, TypeError):
        return None


def _get(cv: dict, path: str) -> str:
    if path == "summary":
        return cv["summary"]
    m = _PATH.match(path)
    return cv[m[1]][int(m[2])]["bullets"][int(m[3])]


def _label(cv: dict, path: str) -> str:
    m = _PATH.match(path)
    if not m:
        return path
    entry = cv[m[1]][int(m[2])]
    return f"{path} ({entry.get('company') or entry.get('name')})"


def build_request(cv: dict, sources: dict, report: LayoutReport, profile: Profile, jd: str, model: str) -> dict:
    index = profile.index()
    parts = [f"<job_description>\n{jd.strip()}\n</job_description>", ""]
    if report.over_pages:
        parts.append(f"The CV renders to {report.pages} pages; the limit is {limits.MAX_PAGES}. "
                     f"About {report.lines_over} lines are on the extra page(s).")
    if report.widows:
        parts.append("Widows to fix:")
        for w in report.widows:
            text = _get(cv, w.path)
            parts += [
                f"- path: {w.path}" + (f"  ({_label(cv, w.path)})" if w.path != "summary" else ""),
                f"  text: {text}",
                f"  {len(text)} characters on {len(w.lines)} lines; the last line holds {w.last_line_words} word(s): "
                f"\"{_strip_glyph(w.lines[-1].text)}\"; a full line holds about {w.chars_per_line} characters",
                "  source bullets:",
                *[f"    {s}" for s in _source_lines(_bullet_sources(sources, w.path), index)],
            ]
    if report.over_pages:
        parts += ["", "All bullets, for choosing what to remove:"]
        parts += [f"- {_label(cv, path)}: {text}" for path, text in _items(cv) if path != "summary"]
    parts += ["", f"Submit the fixes with {TOOL_NAME}."]
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": LAYOUT_EFFORT},
        "tools": [TOOL],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "system": system_prompt(),
        "messages": [{"role": "user", "content": "\n".join(parts)}],
    }


def ask_claude(client, request: dict) -> tuple[RewritePlan, dict]:
    with client.beta.messages.stream(**request) as stream:
        message = stream.get_final_message()
    usage = _usage(message)
    log.info("layout %s: input=%d output=%d stop=%s", message.model, usage["input_tokens"], usage["output_tokens"], message.stop_reason)
    if message.stop_reason in ("refusal", "max_tokens"):
        raise TailorError(f"layout rewrite stopped: {message.stop_reason}")
    call = next((b for b in message.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
    if call is None:
        raise TailorError(f"Claude didn't call {TOOL_NAME}")
    return RewritePlan.model_validate(copy.deepcopy(call.input)), usage


# ---------------------------
# FIX: APPLY AND LOOP
# ---------------------------
def apply_plan(cv: dict, sources: dict, plan: RewritePlan, report: LayoutReport) -> tuple[dict, dict, list[dict]]:
    """Apply rewrites (widows only) and removals (only when over pages); return new cv, sources, changes."""
    cv, sources = copy.deepcopy(cv), copy.deepcopy(sources)
    widows = {w.path for w in report.widows}
    changes = []
    for rw in plan.rewrites:
        if rw.path not in widows:
            changes.append({"path": rw.path, "ignored": "not a flagged widow"})
            continue
        before = _get(cv, rw.path)
        if rw.text.strip() == before:
            continue
        if rw.path == "summary":
            cv["summary"] = rw.text
        else:
            m = _PATH.match(rw.path)
            cv[m[1]][int(m[2])]["bullets"][int(m[3])] = rw.text
        changes.append({"path": rw.path, "before": before, "after": rw.text})
    removals = []
    for path in plan.remove:
        m = _PATH.match(path)
        if not report.over_pages or not m:
            changes.append({"path": path, "ignored": "removal not allowed" if m else "not a bullet path"})
            continue
        try:
            text = cv[m[1]][int(m[2])]["bullets"][int(m[3])]
        except (IndexError, KeyError):
            changes.append({"path": path, "ignored": "no such bullet"})
            continue
        removals.append((m[1], int(m[2]), int(m[3]), path, text))
    for section, i, j, path, text in sorted(removals, key=lambda r: (r[0], r[1], -r[2])):  # delete from the end
        del cv[section][i]["bullets"][j]
        try:
            del sources[section][i]["bullets"][j]
        except (KeyError, IndexError):
            pass
        changes.append({"path": path, "removed": text})
    return cv, sources, changes


def _render_checked(cv: dict, folder: Path, basename: str) -> Path:
    _, pdf = render(cv, folder, basename)
    if not Path(pdf).is_file():
        raise TailorError(f"PDF was not created: {pdf}")
    return Path(pdf)


def fix_layout(cv: dict, sources: dict, profile: dict, jd: str, folder: Path, basename: str, *,
               client=None, model: str | None = None, max_rounds: int | None = None,
               progress=None) -> tuple[dict, dict, dict]:
    """Render, check, and fix widows/page overflow. Returns the best cv, its sources, and meta["layout"].

    The best version is the one left rendered in `folder` when this returns. `max_rounds=0`
    only renders and checks (no Claude call); the default is limits.MAX_LAYOUT_ROUNDS.
    """
    progress = progress or (lambda message: None)
    max_rounds = limits.MAX_LAYOUT_ROUNDS if max_rounds is None else max_rounds
    profile_model = Profile.model_validate(profile)
    model = model or os.environ.get("CV_MODEL") or DEFAULT_MODEL
    progress("Rendering (LibreOffice)")
    pdf = _render_checked(cv, folder, basename)
    report = check_layout(pdf, cv)
    progress(f"Layout check: {report.pages} page(s), {len(report.widows)} widow(s)"
             + (f", over the {limits.MAX_PAGES}-page limit" if report.over_pages else ""))
    rounds = [{"round": 0, **report.summary()}]
    best = (report, cv, sources, 0)
    rendered = 0
    usage_calls = []

    for n in range(1, max_rounds + 1):
        base_report, base_cv, base_sources, base_round = best
        if base_report.ok:
            break
        progress(f"Layout round {n}/{max_rounds}: fixing {len(base_report.widows)} widow(s)"
                 + (" and the page overflow" if base_report.over_pages else ""))
        client = client or anthropic.Anthropic()
        try:
            plan, usage = ask_claude(client, build_request(base_cv, base_sources, base_report, profile_model, jd, model))
        except (TailorError, anthropic.APIError) as exc:  # the CV is already valid; keep the best so far
            rounds.append({"round": n, "based_on": base_round, "result": f"failed: {exc}"})
            log.warning("layout round %d failed: %s", n, exc)
            break
        usage_calls.append(usage)
        new_cv, new_sources, changes = apply_plan(base_cv, base_sources, plan, base_report)
        record = {"round": n, "based_on": base_round, "changes": changes}
        if not any("after" in c or "removed" in c for c in changes):
            rounds.append({**record, "result": "no changes"})
            break
        errors = validate(new_cv, {"sources": new_sources}, profile)
        if errors:
            rounds.append({**record, "result": "rejected", "validation_errors": errors})
            log.warning("layout round %d rejected: %s", n, "; ".join(errors))
            continue
        progress(f"Layout round {n}/{max_rounds}: re-rendering")
        pdf = _render_checked(new_cv, folder, basename)
        rendered = n
        report = check_layout(pdf, new_cv)
        better = report.score() < base_report.score()
        rounds.append({**record, "result": "kept" if better else "worse, discarded", **report.summary()})
        log.info("layout round %d: %d page(s), %d widow(s)%s", n, report.pages, len(report.widows), "" if better else " (worse, discarded)")
        progress(f"Layout round {n}/{max_rounds}: {report.pages} page(s), {len(report.widows)} widow(s)"
                 + ("" if better else ", worse than before, discarded"))
        if better:
            best = (report, new_cv, new_sources, n)

    best_report, best_cv, best_sources, best_round = best
    if rendered != best_round:  # the files on disk are from a discarded round
        progress(f"Re-rendering the kept version (round {best_round})")
        _render_checked(best_cv, folder, basename)
    total = {k: sum(u[k] for u in usage_calls) for k in usage_calls[0]} if usage_calls else {}
    meta = {"kept_round": best_round, "final_pages": best_report.pages, "final_widows": len(best_report.widows),
            "rounds": rounds, "usage": {**total, "calls": usage_calls}}
    return best_cv, best_sources, meta
