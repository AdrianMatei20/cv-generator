# CV Generator

Generates a tailored CV (docx + PDF) from a job description (JD), using a master profile and the Claude API. Built around the original hand-written-JSON renderer, which must keep working on its own.

## Hard rules

1. **`python generate.py` must keep working unchanged, without AI.** Same (no) arguments, same input (`data/*.json`, top level only), same outputs (`output/<json basename>/CV_<NameNoSpaces>.docx|.pdf` and `CV_JohnDoe.docx|.pdf`), no API key, no AI packages. `tests/test_legacy.py` enforces this and must pass after every change: `pytest tests/test_legacy.py`.
2. **`generate.py` and `cvgen/render.py` must never import the AI path** (`anthropic`, `pydantic`, `pymupdf`/`fitz`, `dotenv`, or any `cvgen` module that does). `cvgen/__init__.py` stays import-free for the same reason.
3. **One renderer.** The AI pipeline calls `cvgen.render.render()`. Never copy or fork the docx/PDF logic.
4. **Never invent facts.** The app must not add experience, employers, titles, skills, tools, dates, durations, metrics or certificates that are not in `profile.json`. Claude may select, reorder, shorten and rephrase profile content only. If a JD asks for something the profile lacks, leave it out and report it as a gap.

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
cvgen/profile.py     load profile.json
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

Missing scalar keys render as empty text, not an error, so validation must catch them. `title`, `location`, `workModel` and `contractor` appear in older JSONs for `templates/cv_template.docx` and are ignored by the current template.

## Environment

- Python 3.11, `pip install -r requirements.txt`; LibreOffice installed.
- `ANTHROPIC_API_KEY` in `.env` (see `.env.example`), loaded only by the AI pipeline. `.env` and `output/` are git-ignored.
