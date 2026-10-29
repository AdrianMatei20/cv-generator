# CV Generator

Generates a tailored CV (docx + PDF) from a job description (JD), using a master profile and the Claude API. Built around the original hand-written-JSON renderer, which must keep working on its own.

## Hard rules

1. **`python generate.py` must keep working unchanged, without AI.** Same (no) arguments, same input (`data/*.json`, top level only), same outputs (`output/<json basename>/CV_<NameNoSpaces>.docx|.pdf` and `CV_JohnDoe.docx|.pdf`), no API key, no AI packages. `tests/test_legacy.py` enforces this and must pass after every change: `pytest tests/test_legacy.py`.
2. **`generate.py` and `cvgen/render.py` must never import the AI path** (`anthropic`, `pydantic`, `pymupdf`/`fitz`, `dotenv`, or any `cvgen` module that does). `cvgen/__init__.py` stays import-free for the same reason.
3. **One renderer.** The AI pipeline calls `cvgen.render.render()`. Never copy or fork the docx/PDF logic.
4. **Never invent facts.** The app must not add experience, employers, titles, skills, tools, dates, durations, metrics or certificates that are not in `profile.json`. Claude may select, reorder, shorten and rephrase profile content only. If a JD asks for something the profile lacks, leave it out and report it as a gap.
5. **Everything in a tailored CV is traceable to profile ids.** Every bullet, summary, headline, skill, project, job, education and certification entry must point back to the profile item(s) it came from. Rephrasing and reordering are fine; new facts are not.

## Pipeline

1. `profile.json`: master profile with everything about the candidate (the only source of facts).
2. The user gives the app a JD.
3. `cvgen/tailor.py`: Claude picks and rephrases profile content into a CV JSON matching the schema below.
4. `cvgen/validate.py`: Python validates the JSON (schema + length rules); errors go back to Claude to fix.
5. `cvgen/render.py`: `render()` writes the docx, then the PDF.
6. `cvgen/layout.py`: finds bullets in the PDF whose last line has 4 words or fewer, sends only those to Claude to rephrase, and re-renders. Max 2 rounds.
7. Output is written to `output/<Company>_<Role>_<YYYY-MM-DD>/`:
   - `CV_AdrianMatei.pdf`
   - `CV_AdrianMatei.docx`
   - `cv.json` (the final CV JSON)
   - `job_description.txt` (the JD as given)

## Layout

```
generate.py          legacy command; thin wrapper around cvgen.render
cvgen/render.py      render(cv, out_dir, basename, template=DEFAULT_TEMPLATE) -> (docx_path, pdf_path)
cvgen/schema.py      pydantic CV model (render contract + length limits); CV.to_render_dict()
cvgen/limits.py      all length limits, in one place
cvgen/profile_schema.py  pydantic Profile model
cvgen/profile.py     load_profile(), check_profile(); `python -m cvgen.profile check profile.json`
cvgen/convert.py     one-off: existing CV JSONs -> starter profile.json
cvgen/tailor.py      Claude: profile + JD -> CV JSON
cvgen/validate.py    schema + length rules
cvgen/layout.py      short-last-line detection in the PDF
cvgen/cli.py         AI pipeline entry point
templates/           docx templates (docxtpl / Jinja tags)
data/                hand-written CV JSONs for the legacy command
tests/test_legacy.py golden test for `python generate.py`
tests/golden/        legacy output for tests/fixtures/data/sample.json
```

## Rendering

- Template: `templates/cv_template_with_summary.docx`, filled by docxtpl.
- `render()` escapes `&` as `&amp;` on a copy of the CV, so callers pass plain text. Nothing else is escaped: `<` and `>` produce a broken docx, so validation must reject them.
- `linkedinText`/`linkedinUrl`, `githubText`/`githubUrl` and each project's `linkText`/`linkUrl` become blue underlined hyperlinks (`linkedin`, `github`, `project.link` in the template).
- PDF: LibreOffice `soffice --headless --convert-to pdf <docx> --outdir <dir>`, found on PATH or at `C:\Program Files\LibreOffice\program\soffice.exe`. The exit code is not checked, so the AI pipeline must check the PDF exists.

## CV JSON schema

Used by `templates/cv_template_with_summary.docx`. The AI pipeline must produce every key marked required.

| Key | Type | Notes |
|---|---|---|
| `name` | string | required; also gives the file name (`CV_` + name without spaces) |
| `headline` | string | required |
| `phone`, `email` | string | required |
| `linkedinText`, `linkedinUrl` | string | optional pair; link shown only if both are present |
| `githubText`, `githubUrl` | string | optional pair; same rule |
| `summary` | string | required |
| `skills` | list of `{category: str, skills: str}` | required; `skills` is a comma-separated string |
| `experience` | list of `{title, company, location, period: str, bullets: [str]}` | required |
| `projects` | list of `{name, description: str, linkText?, linkUrl?: str, bullets: [str]}` | required key (the renderer crashes if it is missing; may be an empty list) |
| `education` | list of `{institution, degree, graduated: str}` | required |
| `certificates` | list of `{certificate, date: str, details?: str}` | required; `details` shown only if non-empty |

Missing scalar keys render as empty text, not an error, so validation must catch them. `cvgen.schema.CV` enforces all of this, plus the limits below, forbids unknown keys and rejects `<`/`>`. Always pass `CV.to_render_dict()` (not `model_dump()`) to `render()`: it drops unset optional keys, and a link key set to `None` would crash the renderer.

## Length limits (`cvgen/limits.py`)

Measured on the template: the usable width is 7.5in, text is Calibri 11pt, and a full bullet line holds 102–122 characters (median 113) in the LibreOffice PDF. `CHARS_PER_LINE = 110`. Bullets max 2 lines (220 chars), 6 per role, 4 per project, 3 projects; headline 1 line (110); summary 5 lines (550); 9 skill categories, 60 skills, 2 lines per skill category. Avoid aiming for 115–135-character bullets: they wrap onto a second line holding a word or two. Change limits only in `limits.py`.

## Profile (`profile.json`)

Schema: `cvgen/profile_schema.py`; example: `profile.example.json`. A superset of the CV: `contact`, `headlines` and `summaries` (variants), `experience` (company, title, location, `start`/`end` as `YYYY-MM`, `end: null` = Present, bullets), `projects`, `education`, `certifications` (`date` or `status`), `skills` (groups of named skills), `languages`. Every item has a stable `id` (lowercase-hyphenated, e.g. `exp-acme-03`) and optional `tags`.

- `profile.json` was compiled from all 27 CV JSONs under `data/` (including `data/old/`). Bullets describing the same achievement are merged into one: `text` is the strongest original wording, `facts` holds extra numbers/tech/scope found only in other wordings (each a verbatim excerpt), `angles` says what it demonstrates, `sources` lists the CV files it came from (paths relative to `data/`), or `user-input:<YYYY-MM-DD>` for content the user supplied directly. A project may also have `compact_bullets`: a shorter alternative set where each bullet can summarise several regular ones, so a CV uses `bullets` or `compact_bullets` for a project, never both. Internship entries carry `positions`, and their bullets a `position`. Summaries and headlines have a `target_role`.
- Tailoring may use a bullet's `text` and its `facts`, nothing else. `preferences` (`target_roles`, `never_claim`, `tone`) is filled in by hand and is never a source of facts; anything in `never_claim` must not appear in a CV.
- `python -m cvgen.profile check profile.json`: errors (exit 1) for invalid schema, missing ids and duplicate ids; warnings for bullets with no metric (number, %, "two", "hundreds", ...).
- `load_profile()` is what the pipeline uses; it refuses a profile with errors.
- Ids are permanent: renaming one breaks traceability of CVs generated earlier. Add new ids instead of reusing old ones.
- `python -m cvgen.convert` builds a starter profile from `data/*.json` and `data/old/*.json` (newest wins) and never overwrites an existing file. `title`, `location`, `workModel` and `contractor` appear in older JSONs for `templates/cv_template.docx` and are ignored by the current template.

## Environment

- Python 3.11, `pip install -r requirements.txt`; LibreOffice installed.
- `ANTHROPIC_API_KEY` in `.env` (see `.env.example`), loaded only by the AI pipeline. `.env` and `output/` are git-ignored.
