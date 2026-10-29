"""One-off: build a starter profile.json from existing hand-written CV JSONs.

    python -m cvgen.convert                      # data/*.json + data/old/*.json -> profile.json
    python -m cvgen.convert a.json b.json -o out.json

Inputs are read newest first (file modification time); the newest value wins
for contact details, job titles, locations and project links. Jobs from
different files are merged when their titles match (ignoring "(Part-time)"
etc.) and their dates overlap. Bullets, summaries, headlines and skills are
deduplicated by normalised text, so near-duplicate rewordings remain for you to
prune. Nothing is invented: every string comes from an input file. Languages
are left empty because the CV JSONs don't list them.

Never overwrites an existing output file.
"""

import argparse
import re
import sys
from pathlib import Path

from cvgen.profile import check_profile, read_json
from cvgen.profile_schema import MONTHS
from cvgen.schema import split_list

DEFAULT_INPUT_DIRS = (Path("data"), Path("data/old"))
_MONTH_NUMBERS = {name.lower(): i for i, name in enumerate(MONTHS, 1)}


def slug(text: str, max_words: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:max_words]) or "item"


def strip_parens(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", text).strip()


def norm(text: str) -> str:
    """Dedup key: case, dashes, spacing and trailing punctuation don't matter."""
    text = text.lower().replace("‑", "-").replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", text).strip().rstrip(".")


def parse_month(text: str):
    """'Sept 2025' / 'June 2023' -> '2025-09'; 'Present' -> None."""
    text = text.strip()
    if text.lower() in ("present", "now", "current", ""):
        return None
    match = re.match(r"([A-Za-z]+)\.?\s+(\d{4})$", text)
    if not match or match[1][:3].lower() not in _MONTH_NUMBERS:
        raise ValueError(f"can't parse date '{text}'")
    return f"{match[2]}-{_MONTH_NUMBERS[match[1][:3].lower()]:02d}"


def parse_period(period: str):
    parts = re.split(r"\s+[–—-]\s+", period.strip())
    start = parse_month(parts[0])
    end = parse_month(parts[1]) if len(parts) > 1 else start
    return start, end


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start <= (b_end or "9999-12") and b_start <= (a_end or "9999-12")


class _Ids:
    def __init__(self):
        self.used = set()

    def take(self, base: str) -> str:
        candidate, n = base, 2
        while candidate in self.used:
            candidate, n = f"{base}-{n}", n + 1
        self.used.add(candidate)
        return candidate


def _add_unique(items: list, seen: set, text: str, **extra):
    key = norm(text)
    if key and key not in seen:
        seen.add(key)
        items.append({"text": text.strip(), **extra})


def build_profile(cvs: list[dict]) -> dict:
    """Merge CV dicts (newest first) into a profile dict."""
    contact, headlines, summaries = {}, [], []
    seen_headlines, seen_summaries = set(), set()
    jobs, projects, education, certs = [], {}, {}, {}
    groups, seen_skills = {}, set()

    for cv in cvs:
        for key in ("name", "phone", "email", "location"):
            if cv.get(key) and key not in contact:
                contact[key] = cv[key]
        for key in ("linkedin", "github"):
            if cv.get(f"{key}Text") and cv.get(f"{key}Url") and key not in contact:
                contact[key] = {"text": cv[f"{key}Text"], "url": cv[f"{key}Url"]}

        _add_unique(headlines, seen_headlines, cv.get("headline") or cv.get("title") or "")
        _add_unique(summaries, seen_summaries, cv.get("summary") or "")

        for job in cv.get("experience", []):
            start, end = parse_period(job["period"])
            title_key = norm(strip_parens(job["title"]))
            match = next(
                (j for j in jobs if j["_title"] == title_key and _overlaps(start, end, j["start"], j["end"])),
                None,
            )
            if match is None:
                match = {
                    "_title": title_key, "_seen": set(), "company": job["company"], "title": job["title"],
                    "location": job.get("location", ""), "start": start, "end": end, "bullets": [],
                }
                jobs.append(match)
            for bullet in job.get("bullets", []):
                _add_unique(match["bullets"], match["_seen"], bullet)

        for project in cv.get("projects", []):
            key = norm(strip_parens(project["name"]))
            entry = projects.setdefault(key, {
                "_seen": set(), "name": strip_parens(project["name"]) or project["name"],
                "description": project.get("description") or "", "link": None, "bullets": [],
            })
            if not entry["description"] and project.get("description"):
                entry["description"] = project["description"]
            if entry["link"] is None and project.get("linkText") and project.get("linkUrl"):
                entry["link"] = {"text": project["linkText"], "url": project["linkUrl"]}
            for bullet in project.get("bullets", []):
                _add_unique(entry["bullets"], entry["_seen"], bullet)

        for edu in cv.get("education", []):
            education.setdefault(norm(edu["degree"]), {
                "institution": edu["institution"], "degree": edu["degree"],
                "graduated": parse_month(edu["graduated"]),
            })

        for cert in cv.get("certificates", []):
            name = cert["certificate"]
            entry = {"name": strip_parens(name) if "progress" in name.lower() else name}
            date = (cert.get("date") or "").strip()
            try:
                if parse_month(date):
                    entry["date"] = parse_month(date)
            except ValueError:
                entry["status"] = date
            if "status" not in entry and "date" not in entry and "progress" in name.lower():
                entry["status"] = "In progress"
            if cert.get("details"):
                entry["details"] = cert["details"]
            certs.setdefault(norm(strip_parens(name)), entry)

        for line in cv.get("skills", []):
            group = groups.setdefault(line["category"], [])
            for skill in split_list(line["skills"]):
                skill = re.sub(r"^(?:SQL|NoSQL)\s*:\s*", "", skill)
                key = norm(strip_parens(skill))
                if key and key not in seen_skills:
                    seen_skills.add(key)
                    group.append(skill)

    if "location" not in contact:
        contact.pop("location", None)
    ids = _Ids()

    def bullets(prefix, texts):
        return [{"id": ids.take(f"{prefix}-{i:02d}"), "text": b["text"]} for i, b in enumerate(texts, 1)]

    profile_jobs = []
    for job in jobs:
        job_id = ids.take("exp-" + slug(re.split(r"[|,]", job["company"])[0], 3))
        profile_jobs.append({
            "id": job_id, "company": job["company"], "title": job["title"], "location": job["location"],
            "start": job["start"], "end": job["end"], "bullets": bullets(job_id, job["bullets"]),
        })

    profile_projects = []
    for project in projects.values():
        project_id = ids.take("proj-" + slug(project["name"], 3))
        item = {"id": project_id, "name": project["name"], "description": project["description"] or project["name"]}
        if project["link"]:
            item["link"] = project["link"]
        item["bullets"] = bullets(project_id, project["bullets"])
        profile_projects.append(item)

    return {
        "contact": contact,
        "headlines": [{"id": ids.take(f"headline-{i:02d}"), "text": h["text"]} for i, h in enumerate(headlines, 1)],
        "summaries": [{"id": ids.take(f"summary-{i:02d}"), "text": s["text"]} for i, s in enumerate(summaries, 1)],
        "experience": profile_jobs,
        "projects": profile_projects,
        "education": [{"id": ids.take("edu-" + slug(e["degree"].split(" in ")[0], 3)), **e} for e in education.values()],
        "certifications": [{"id": ids.take("cert-" + slug(strip_parens(c["name"]), 6)), **c} for c in certs.values()],
        "skills": [
            {"category": category, "skills": [{"id": ids.take("skill-" + slug(strip_parens(s) or s, 3)), "name": s} for s in names]}
            for category, names in groups.items() if names
        ],
        "languages": [],
    }


def default_inputs() -> list[Path]:
    files = [p for d in DEFAULT_INPUT_DIRS if d.is_dir() for p in d.glob("*.json")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def main(argv=None) -> int:
    import json

    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m cvgen.convert", description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="*", type=Path, help="CV JSON files, newest first (default: data/ and data/old/ by date)")
    parser.add_argument("-o", "--output", type=Path, default=Path("profile.json"))
    args = parser.parse_args(argv)

    if args.output.exists():
        print(f"{args.output} already exists; not overwriting it. Pass -o to write somewhere else.")
        return 1
    inputs = args.inputs or default_inputs()
    if not inputs:
        print("No CV JSON files found.")
        return 1

    profile = build_profile([read_json(p) for p in inputs])
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
        f.write("\n")

    result = check_profile(profile)
    n_bullets = sum(len(j["bullets"]) for j in profile["experience"]) + sum(len(p["bullets"]) for p in profile["projects"])
    n_skills = sum(len(g["skills"]) for g in profile["skills"])
    print(f"Read {len(inputs)} file(s), newest first: {', '.join(p.name for p in inputs[:3])}{', ...' if len(inputs) > 3 else ''}")
    print(
        f"Wrote {args.output}: {len(profile['headlines'])} headlines, {len(profile['summaries'])} summaries, "
        f"{len(profile['experience'])} jobs, {len(profile['projects'])} projects, {n_bullets} bullets, "
        f"{len(profile['skills'])} skill groups ({n_skills} skills)"
    )
    print(f"Check: {len(result.errors)} error(s), {len(result.warnings)} bullet(s) with no metric")
    for line in result.errors:
        print(f"ERROR  {line}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
