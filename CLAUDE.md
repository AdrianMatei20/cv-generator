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
3. `cvgen/tailor.py`: Claude picks and rephrases profile content into a CV JSON matching the schema below. `tailor(profile, jd) -> TailorResult` (`cv`, `company`, `role`, `sources`, `gaps`, `problems`, `usage`; `meta()` is what goes in `cv.meta.json`).
   - Model from `CV_MODEL` (default `claude-opus-5-5`), effort from `CV_EFFORT` (default `high`; Opus 5.5's own default is `medium`). Adaptive thinking, streaming, and the server-side refusal fallback (`fallbacks: "default"`) are on.
   - Structured output via one strict tool, `submit_cv` (schema from `TailoredCV`). Opus 5.5 rejects a forced `tool_choice`, so it's `auto` + the prompt, and `tailor()` retries once if no tool call comes back.
   - Claude only picks profile items by id and writes headline, summary, skill lines and bullet text, each with `source_ids`. Titles, companies, dates, locations, education, certificates and contact details are copied from the profile by id in `assemble()`, which also reports unknown or misplaced ids, mixed compact/regular project bullets, `never_claim` hits and schema/limit violations in `problems`.
   - Caching: tools + system prompt + profile (without `sources`) form a byte-stable prefix with `cache_control` on the last system block; the JD is the only uncached input. Never put anything per-run (dates, ids, the JD) before that breakpoint. Usage incl. `cache_read` is logged at INFO.
   - `python samples/try_tailor.py` runs it on `samples/jd_example.txt` (real API call).
4. `cvgen/validate.py`: `validate(cv, meta, profile) -> list[str]` (empty = OK). Checks the pydantic schema; length limits; traceability (every source id exists, bullets come from their own job/project, never mixing a project's bullets and compact_bullets, contact/titles/companies/dates/locations/education/certificates equal the profile exactly, every listed skill and qualifier is in the profile); numbers (every number in a bullet, headline or summary appears in its source items, spelled-out numbers like "two" count); hygiene (no empty strings, leading/trailing/double spaces, duplicate bullets, first person) and `preferences.never_claim`. Errors are one readable line each, addressed by rendered-CV path, e.g. `experience[1] (Ruuts.travel).bullets[2]: ...`.
   - Repair loop in `tailor()`: on errors, the assistant turn (unchanged, thinking included) and an `is_error` `tool_result` listing the errors go back in the same conversation, and Claude calls `submit_cv` again. At most `MAX_REPAIRS = 2` repairs; then `TailorValidationError`, with the last attempt saved as `cv.draft.json` + `cv.draft.meta.json` in `draft_dir` if given. `result.repairs` / `meta["repairs"]` record each rejected attempt's errors.
5. `cvgen/render.py`: `render()` writes the docx, then the PDF.
6. `cvgen/layout.py`: `check_layout(pdf, cv)` reads the PDF with PyMuPDF, groups spans into lines by baseline, and maps the summary and every bullet to its lines by matching whitespace-free normalised text in document order (bullet glyphs, ligatures, soft hyphens and hyphenated line breaks handled; anything unmapped is logged and reported, never fatal). A wrapped item whose last line has `WIDOW_MAX_WORDS` (4) words or fewer is a widow; more than `MAX_PAGES` pages is an overflow. `fix_layout()` sends only the flagged items, each with its source profile bullets, chars-per-line and last-line words, to Claude via a small strict tool `rewrite_bullets` (effort `medium`): it rewrites widows (shorter by a line, or fuller with true source detail) and, only when over the page limit, lists the least relevant bullets to remove. Each round runs `validate()` (rejected rounds are recorded, not rendered), re-renders and re-checks; at most `MAX_LAYOUT_ROUNDS` (2) rounds, always from the best version so far, scored by (over the page limit, widows, pages). The best version is the one left on disk and in `cv.json`; `meta["layout"]` records every round's widows, changes, validation errors, result, and the token usage. A failed rewrite call ends the loop but keeps the CV.
7. `cvgen/pipeline.py` `run(jd, company=None, role=None) -> Path` ties it together (CLI: `python -m cvgen.cli jd.txt [--company X --role Y]`, or `-` for stdin). Explicit company/role override what Claude extracted (`meta["extracted"]` keeps the originals). Names are sanitised by `cvgen/output.py`: accents stripped, PascalCase, only letters/digits, max 40 chars each, `Unknown` if empty; the date is today's local date; an existing folder gets `_2`, `_3`, ... instead of being overwritten. File base name is `CV_<CANDIDATE_FILENAME>` (env, default `AdrianMatei`). The PDF comes from the same LibreOffice `render()` as the legacy command, and `run()` fails if it wasn't created. If repairs fail, the folder holds `job_description.txt`, `cv.draft.json` and `cv.draft.meta.json` instead. Output is written to `output/<Company>_<Role>_<YYYY-MM-DD>/`:
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
cvgen/output.py      output folder naming (sanitising, date, _N suffix), CANDIDATE_FILENAME
cvgen/pipeline.py    run(jd, company, role) -> output folder
cvgen/cli.py         AI pipeline entry point: python -m cvgen.cli jd.txt
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

Measured on the template: the usable width is 7.5in, text is Calibri 11pt, and a full bullet line holds 102–122 characters (median 113) in the LibreOffice PDF. `CHARS_PER_LINE = 110`. Bullets max 2 lines (220 chars), 6 per role, 4 per project, 3 projects; headline 1 line (110); summary 5 lines (550); 9 skill categories, 60 skills, 2 lines per skill category; `MAX_PAGES = 2`; layout: `WIDOW_MAX_WORDS = 4`, `MAX_LAYOUT_ROUNDS = 2`. Avoid aiming for 115–135-character bullets: they wrap onto a second line holding a word or two. Change limits only in `limits.py`.

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
