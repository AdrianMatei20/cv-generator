"""Validate a CV JSON against the render schema, the length limits, and the profile it came from.

    errors = validate(cv, meta, profile)   # [] when the CV is good to render

`cv` is the render dict (cv.json), `meta` is TailorResult.meta() (cv.meta.json; only
`meta["sources"]` is used) and `profile` is profile.json as a dict. Each error is one
human-readable line naming where in the CV the problem is; the same lines go back to
Claude in the repair loop, so they say what to change.
"""

import re

from pydantic import ValidationError

from cvgen import limits
from cvgen.profile_schema import Bullet, Certification, Education, Job, Profile, Project, format_month
from cvgen.schema import CV, split_list

# Pydantic error types already reported, with clearer wording, by the length checks below.
_LENGTH_ERRORS = {"string_too_long", "too_long"}
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?!\w)")
_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                 "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}
_FIRST_PERSON = re.compile(r"\b(?:I(?![/\w])|me|my|mine|myself|we|our|ours|us)\b")


def validate(cv: dict, meta: dict, profile: dict) -> list[str]:
    profile_model = Profile.model_validate(profile)
    errors = _schema_errors(cv)
    if not isinstance(cv, dict):
        return errors
    errors += _length_errors(cv)
    errors += _traceability_errors(cv, meta.get("sources", {}), profile_model)
    errors += _hygiene_errors(cv, profile_model)
    return errors


# ---------------------------
# SCHEMA AND LENGTHS
# ---------------------------
def _schema_errors(cv) -> list[str]:
    try:
        CV.model_validate(cv)
    except ValidationError as exc:
        return [f"{_path(err['loc'])}: {err['msg']}" for err in exc.errors() if err["type"] not in _LENGTH_ERRORS]
    return []


def _path(loc) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(cv)"


def _label(cv, section, i):
    """'experience[1] (Ruuts.travel)': readable for a person and unambiguous for Claude."""
    item = cv[section][i]
    name = item.get("company") or item.get("name") or item.get("category") if isinstance(item, dict) else None
    return f"{section}[{i}] ({name})" if name else f"{section}[{i}]"


def _items(cv, section):
    value = cv.get(section)
    return [(i, item) for i, item in enumerate(value) if isinstance(item, dict)] if isinstance(value, list) else []


def _length_errors(cv) -> list[str]:
    errors = []

    def text_limit(where, value, limit):
        if isinstance(value, str) and len(value) > limit:
            errors.append(f"{where} is {len(value)} characters; max {limit}. Shorten it.")

    text_limit("headline", cv.get("headline"), limits.MAX_HEADLINE_CHARS)
    text_limit("summary", cv.get("summary"), limits.MAX_SUMMARY_CHARS)
    for section, max_bullets in (("experience", limits.MAX_BULLETS_PER_ROLE), ("projects", limits.MAX_BULLETS_PER_PROJECT)):
        for i, item in _items(cv, section):
            bullets = item.get("bullets") if isinstance(item.get("bullets"), list) else []
            if len(bullets) > max_bullets:
                errors.append(f"{_label(cv, section, i)} has {len(bullets)} bullets; max {max_bullets}. Drop the least relevant.")
            for j, bullet in enumerate(bullets):
                text_limit(f"{_label(cv, section, i)}.bullets[{j}]", bullet, limits.MAX_BULLET_CHARS)
    projects = cv.get("projects")
    if isinstance(projects, list) and len(projects) > limits.MAX_PROJECTS:
        errors.append(f"projects has {len(projects)} entries; max {limits.MAX_PROJECTS}. Drop the least relevant.")
    skills = _items(cv, "skills")
    if len(skills) > limits.MAX_SKILL_CATEGORIES:
        errors.append(f"skills has {len(skills)} categories; max {limits.MAX_SKILL_CATEGORIES}. Merge or drop some.")
    total = 0
    for i, line in skills:
        names = split_list(line.get("skills") or "")
        total += len(names)
        length = len(line.get("category") or "") + 2 + len(line.get("skills") or "")
        if length > limits.MAX_SKILL_LINE_CHARS:
            errors.append(f"{_label(cv, 'skills', i)} line is {length} characters; max {limits.MAX_SKILL_LINE_CHARS}. Remove some skills.")
    if total > limits.MAX_SKILLS_TOTAL:
        errors.append(f"skills lists {total} skills; max {limits.MAX_SKILLS_TOTAL}. Remove the least relevant.")
    return errors


# ---------------------------
# TRACEABILITY
# ---------------------------
def _source_text(item) -> str:
    """All wording a CV text may draw on from one profile item (e.g. a bullet's text and facts)."""
    parts = []

    def walk(value, key=None):
        if key in ("id", "sources", "tags", "angles"):
            return
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(item.model_dump())
    return " ".join(parts)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def _source_numbers(text: str) -> set[str]:
    words = {_NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", text.lower()) if w in _NUMBER_WORDS}
    return _numbers(text) | words


def _traceability_errors(cv, sources, profile: Profile) -> list[str]:
    errors = []
    index = profile.index()

    def ids_exist(ids, where) -> list:
        if not isinstance(ids, list) or not ids:
            errors.append(f"{where}: no source ids")
            return []
        found = []
        for item_id in ids:
            if item_id in index:
                found.append(index[item_id])
            else:
                errors.append(f"{where}: source id '{item_id}' doesn't exist in profile.json")
        return found

    def check_numbers(text, items, where):
        if not isinstance(text, str):
            return
        available = set().union(*(_source_numbers(_source_text(it)) for it in items)) if items else set()
        invented = sorted(_numbers(text) - available, key=lambda n: (len(n), n))
        if invented and items:
            errors.append(f"{where}: number(s) {', '.join(invented)} not found in its source items; use only numbers the profile gives")

    def same(where, field, actual, expected):
        if actual != expected:
            errors.append(f"{where}.{field} is '{actual}' but the profile says '{expected}'")

    # contact
    c = profile.contact
    for field, expected in (("name", c.name), ("phone", c.phone), ("email", c.email),
                            ("linkedinText", c.linkedin.text if c.linkedin else None), ("linkedinUrl", c.linkedin.url if c.linkedin else None),
                            ("githubText", c.github.text if c.github else None), ("githubUrl", c.github.url if c.github else None)):
        if cv.get(field) != expected:
            errors.append(f"{field} is '{cv.get(field)}' but the profile says '{expected}'")

    # headline and summary
    for key in ("headline", "summary"):
        check_numbers(cv.get(key), ids_exist(sources.get(key), key), key)

    # skills: every listed skill must be a profile skill
    skill_by_name = {}
    for group in profile.skills:
        for skill in group.skills:
            skill_by_name[skill.name.lower()] = skill
    skill_sources = sources.get("skills") or []
    for i, line in _items(cv, "skills"):
        where = _label(cv, "skills", i)
        ids_exist(skill_sources[i] if i < len(skill_sources) else None, where)
        for name in split_list(line.get("skills") or ""):
            m = re.match(r"^(.*?)\s*\((.*)\)\s*$", name)
            base, qualifier = (m[1], m[2]) if m else (name, "")
            skill = skill_by_name.get(base.lower())
            if skill is None:
                errors.append(f"{where}: skill '{base}' isn't in the profile's skills")
            elif qualifier and qualifier.lower() not in " ".join(skill.facts).lower():
                errors.append(f"{where}: '{name}' adds '{qualifier}', which the profile doesn't record for {skill.name}")

    # experience and projects: entries match the profile; bullets come from their own entry
    for section, kind, fields in (
        ("experience", Job, lambda job: {"title": job.title, "company": job.company, "location": job.location, "period": job.period}),
        ("projects", Project, lambda p: {"name": p.name, "description": p.description,
                                         "linkText": p.link.text if p.link else None, "linkUrl": p.link.url if p.link else None}),
    ):
        entries = _items(cv, section)
        meta_entries = sources.get(section) or []
        if len(meta_entries) != len(cv.get(section) or []):
            errors.append(f"cv.meta.json sources.{section} has {len(meta_entries)} entries but the CV has {len(cv.get(section) or [])}")
            continue
        for i, entry in entries:
            where = _label(cv, section, i)
            src = meta_entries[i] if isinstance(meta_entries[i], dict) else {}
            item = index.get(src.get("id"))
            if not isinstance(item, kind):
                errors.append(f"{where}: source id '{src.get('id')}' is not a profile {kind.__name__.lower()}")
                continue
            for field, expected in fields(item).items():
                same(where, field, entry.get(field), expected)
            own = {b.id: b for b in item.bullets}
            compact = {b.id: b for b in getattr(item, "compact_bullets", [])}
            bullet_sources = src.get("bullets") or []
            bullets = entry.get("bullets") if isinstance(entry.get("bullets"), list) else []
            if len(bullet_sources) != len(bullets):
                errors.append(f"{where}: cv.meta.json lists sources for {len(bullet_sources)} bullets but the CV has {len(bullets)}")
                continue
            used = set()
            for j, (text, ids) in enumerate(zip(bullets, bullet_sources)):
                bwhere = f"{where}.bullets[{j}]"
                found = ids_exist(ids, bwhere)
                for item_id in ids or []:
                    if item_id in index and item_id not in own and item_id not in compact:
                        errors.append(f"{bwhere}: source id '{item_id}' belongs to another entry, not {item.id}")
                used.update(ids or [])
                check_numbers(text, [f for f in found if isinstance(f, Bullet)], bwhere)
            if used & set(own) and used & set(compact):
                errors.append(f"{where}: mixes its bullets and compact_bullets; use one set or the other")

    # education and certificates match their profile entries
    for section, kind in (("education", Education), ("certificates", Certification)):
        ids = sources.get(section) or []
        if len(ids) != len(cv.get(section) or []):
            errors.append(f"cv.meta.json sources.{section} has {len(ids)} entries but the CV has {len(cv.get(section) or [])}")
            continue
        for i, entry in _items(cv, section):
            item = index.get(ids[i])
            where = f"{section}[{i}]"
            if not isinstance(item, kind):
                errors.append(f"{where}: source id '{ids[i]}' is not a profile {kind.__name__.lower()}")
            elif kind is Education:
                for field, expected in (("institution", item.institution), ("degree", item.degree), ("graduated", format_month(item.graduated))):
                    same(where, field, entry.get(field), expected)
            else:
                date = format_month(item.date) if item.date else (item.status or "")
                for field, expected in (("certificate", item.name), ("date", date), ("details", item.details)):
                    same(where, field, entry.get(field), expected)
    return errors


# ---------------------------
# HYGIENE
# ---------------------------
def _strings(node, path="(cv)"):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, k if path == "(cv)" else f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def _hygiene_errors(cv, profile: Profile) -> list[str]:
    errors = []
    for path, value in _strings(cv):
        if not value.strip():
            if not re.fullmatch(r"certificates\[\d+\]\.date", path):  # a certificate may have no date
                errors.append(f"{path} is empty")
        elif value != value.strip():
            errors.append(f"{path} has leading or trailing spaces")
        elif "  " in value:
            errors.append(f"{path} has a double space")
    seen = {}
    for section in ("experience", "projects"):
        for i, entry in _items(cv, section):
            for j, bullet in enumerate(entry.get("bullets") or []):
                if not isinstance(bullet, str):
                    continue
                where = f"{_label(cv, section, i)}.bullets[{j}]"
                key = re.sub(r"\W+", " ", bullet.lower()).strip()
                if key in seen:
                    errors.append(f"{where} duplicates {seen[key]}")
                else:
                    seen[key] = where
                if _FIRST_PERSON.search(bullet):
                    errors.append(f"{where} uses first person ('{_FIRST_PERSON.search(bullet)[0]}'); rewrite without it")
    for key in ("headline", "summary"):
        if isinstance(cv.get(key), str) and _FIRST_PERSON.search(cv[key]):
            errors.append(f"{key} uses first person ('{_FIRST_PERSON.search(cv[key])[0]}'); rewrite without it")
    text = " ".join(v for _, v in _strings(cv)).lower()
    for phrase in profile.preferences.never_claim:
        if phrase and phrase.lower() in text:
            errors.append(f"the CV mentions '{phrase}', which preferences.never_claim forbids")
    return errors
