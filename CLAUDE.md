# CV Generator

Generates a tailored CV (docx + PDF) from a job description (JD), using a master profile and the Claude API. Built around the original hand-written-JSON renderer, which must keep working on its own.

## Hard rules

1. **`python generate.py` must keep working unchanged, without AI.** Same (no) arguments, same input (`data/*.json`, top level only), same outputs (`output/<json basename>/CV_<NameNoSpaces>.docx|.pdf` and `CV_JohnDoe.docx|.pdf`), no API key, no AI packages. `tests/test_legacy.py` enforces this and must pass after every change: `pytest tests/test_legacy.py`.
2. **`generate.py` and `cvgen/render.py` must never import the AI path** (`anthropic`, `pydantic`, `pymupdf`/`fitz`, `dotenv`, or any `cvgen` module that does). `cvgen/__init__.py` stays import-free for the same reason.
3. **One renderer.** The AI pipeline calls `cvgen.render.render()`. Never copy or fork the docx/PDF logic.
4. **Never invent facts.** The app must not add experience, employers, titles, skills, tools, dates, durations, metrics or certificates that are not in `profile.json`. Claude may select, reorder, shorten and rephrase profile content only. If a JD asks for something the profile lacks, leave it out and report it as a gap.
5. **Everything in a tailored CV is traceable to profile ids.** Every bullet, summary, headline, availability line, skill, project, job, education and certification entry must point back to the profile item(s) it came from. Rephrasing and reordering are fine; new facts are not.

## Pipeline

1. `profile.json`: master profile with everything about the candidate (the only source of facts).
2. The user gives the app a JD.
3. `cvgen/tailor.py`: Claude picks and rephrases profile content into a CV JSON matching the schema below. `tailor(profile, jd) -> TailorResult` (`cv`, `company`, `role`, `sources`, `gaps`, `problems`, `usage`; `meta()` is what goes in `cv.meta.json`).
   - Model from `TAILOR_MODEL`, effort from `TAILOR_EFFORT` (see 12; `CV_MODEL`/`CV_EFFORT` still work as older names). `MAX_TOKENS = 16000`. Adaptive thinking, streaming, and the server-side refusal fallback (`fallbacks: "default"`) are on.
   - Structured output via one strict tool, `submit_cv` (schema from `TailoredCV`). Opus 5.5 rejects a forced `tool_choice`, so it's `auto` + the prompt, and `tailor()` retries once if no tool call comes back.
   - Claude only picks profile items by id and writes headline, summary, skill lines and bullet text, each with `source_ids`. Titles, companies, dates, locations, education, certificates, contact details and the availability line (`availability_ids` -> `preferences.availability` texts joined with " | ", key `availability`, omitted when none) are copied from the profile by id in `assemble()`, which also reports unknown or misplaced ids, mixed compact/regular project bullets, `never_claim` hits and schema/limit violations in `problems`.
   - Caching: tools + system prompt + profile (`profile_block()`: minified, keys sorted, without `contact`, `sources` and `link`, which Claude never needs) form a byte-stable prefix with `cache_control` on the last system block; the JD is the only uncached input. Never put anything per-run (dates, ids, the JD) before that breakpoint. Usage incl. `cache_read` is logged at INFO.
   - `python samples/try_tailor.py` runs it on `samples/jd_example.txt` (real API call).
4. `cvgen/validate.py`: `validate(cv, meta, profile) -> list[str]` (empty = OK). Checks the pydantic schema; length limits; traceability (every source id exists, bullets come from their own job/project, never mixing a project's bullets and compact_bullets, contact/titles/companies/dates/locations/education/certificates equal the profile exactly, every listed skill and qualifier is in the profile); numbers (every number in a bullet, headline or summary appears in its source items, spelled-out numbers like "two" count); hygiene (no empty strings, leading/trailing/double spaces, duplicate bullets, first person) and `preferences.never_claim`. Errors are one readable line each, addressed by rendered-CV path, e.g. `experience[1] (Ruuts.travel).bullets[2]: ...`.
   - Before validating, `python_fixes()` handles the one limit error that's a pure removal: over 60 skills, the largest category loses its last skill (recorded in `meta["python_fixes"]`, saving a repair call). Text over a length limit (headline, summary, bullets) always goes back to Claude for a repair; never cut content in Python.
   - Availability (relocation, remote/hybrid) is its own line under the headline, outside the headline limit (`MAX_AVAILABILITY_CHARS`, one line), so it's never trimmed to make the headline fit. Validation checks it equals the picked profile lines and that the headline doesn't repeat any of them. The template shows it with `{%p if availability %}` paragraphs, which docxtpl removes when it's absent, so legacy output is unchanged.
   - Repair loop in `tailor()`: on errors, the assistant turn (unchanged, thinking included) and an `is_error` `tool_result` listing the errors go back in the same conversation, and Claude calls `submit_cv` again. At most `MAX_REPAIRS = 2` repairs; then `TailorValidationError`, with the last attempt saved as `cv.draft.json` + `cv.draft.meta.json` in `draft_dir` if given. `result.repairs` / `meta["repairs"]` record each rejected attempt's errors.
5. `cvgen/render.py`: `render()` writes the docx, then the PDF.
6. `cvgen/layout.py`: `check_layout(pdf, cv)` reads the PDF with PyMuPDF, groups spans into lines by baseline, and maps the summary and every bullet to its lines by matching whitespace-free normalised text in document order (bullet glyphs, ligatures, soft hyphens and hyphenated line breaks handled; anything unmapped is logged and reported, never fatal). A wrapped item whose last line has `WIDOW_MAX_WORDS` (4) words or fewer is a widow; more than `MAX_PAGES` pages is an overflow. `fix_layout()` first handles page overflow in Python, no API call: `trim_bullets()` drops the last (lowest-ranked) bullet of the entry with the most bullets (later entries first on ties, never an entry's only bullet) until about `lines_over` lines are freed, re-renders and re-checks, up to 3 passes (rounds `trim N`). Then widows go to `LAYOUT_MODEL` (see 12) via a small strict tool `rewrite_bullets`: only the widows, each with its source profile bullets, chars-per-line and last-line words (never the profile, the JD or other bullets); it rewrites them (shorter by a line, or fuller with true source detail). The refusal fallback is sent only to models that support it (`settings.supports_fallbacks()`; not Haiku). Each round runs `validate()` (rejected rounds are recorded, not rendered), re-renders and re-checks; at most `MAX_LAYOUT_ROUNDS` (2) rounds, always from the best version so far, scored by (over the page limit, widows, pages). The best version is the one left on disk and in `cv.json`; `meta["layout"]` records every round's widows, changes, validation errors, result, and the token usage. A failed rewrite call ends the loop but keeps the CV.
7. `cvgen/pipeline.py` `run(jd, company=None, role=None, *, pages=None, layout_fix=True, model=None, progress=None) -> Path` ties it together. `pages` overrides `limits.MAX_PAGES` for the run via `limits.override()` (prompts read limits at request time, so never cache limit values at import); `layout_fix=False` renders and checks without Claude (`fix_layout(max_rounds=0)`); `progress` is a callback that `tailor()` and `fix_layout()` call with one-line stage messages. Before any API call `run()` checks the profile (`load_profile_checked`), the API key and LibreOffice and raises `SetupError` with a fix-it message. The client is created with `max_retries=6`, so the SDK backs off on 429/529/5xx. `meta["cost_usd"]` is the sum of the per-call rows (see 11). `rerender(folder)` re-renders a hand-edited `cv.json` in place (same `CV_*` file name), runs `validate()` and the layout check (no API call), reports validation errors without failing, and appends to `meta["rerenders"]`. Explicit company/role override what Claude extracted (`meta["extracted"]` keeps the originals). Names are sanitised by `cvgen/output.py`: accents stripped, PascalCase, only letters/digits, max 40 chars each, `Unknown` if empty; the date is today's local date; an existing folder gets `_2`, `_3`, ... instead of being overwritten. File base name is `CV_<CANDIDATE_FILENAME>` (env, default `AdrianMatei`). The PDF comes from the same LibreOffice `render()` as the legacy command, and `run()` fails if it wasn't created. If repairs fail, the folder holds `job_description.txt`, `cv.draft.json` and `cv.draft.meta.json` instead. Output is written to `output/<Company>_<Role>_<YYYY-MM-DD>/`:
   - `CV_AdrianMatei.pdf`
   - `CV_AdrianMatei.docx`
   - `CV_JohnDoe.pdf` / `CV_JohnDoe.docx`: the final CV through `cvgen.render.anonymize()` (same as `generate.py`), rendered by `pipeline.render_anonymized()` after the layout step and by `rerender()`; not layout-checked. `ANON_BASENAME = "CV_JohnDoe"`; anything that picks "the CV" from a folder must skip it (server `pdf`/`docx` vs `anon_pdf`/`anon_docx`, CLI `_pdf()`, `rerender()`'s base name).
   - `cv.json` (the final CV JSON)
   - `job_description.txt` (the JD as given)
   - `cv.meta.json` (sources, gaps, repairs, layout rounds, usage, cost, model, settings)
9. `cvgen/gaps.py`: skill-gap report, run by `run()` after the layout step unless `gaps=False` (`--no-gaps`); a failure is recorded in `meta["gap_report"]["error"]`, never fatal. `gap_report(profile, jd)` -> `GapResult`; tool `submit_gap_report` (schema `GapReport`: fit {score 0-100, justification, summary}; requirements {id, name, skill = canonical cross-JD label, category, importance must-have/nice-to-have, status have/partial/missing, evidence ids, note}; recommendations {requirement_ids, type course/certification/project/reading/practice, title, provider, why, effort_hours, cost, project_idea + github for projects, url}; quick_wins <= 3). `validate_report()`: evidence ids exist; have/partial need evidence, missing has none; every recommendation points at partial/missing requirements and every partial/missing requirement is covered; projects have an idea and a GitHub plan. Same repair loop as tailoring (`MAX_REPAIRS`). Output: `gap_report.json`/`.md` in the run folder, and `update_backlog()` rebuilds `output/skills_backlog.md` from every `gap_report.json` (grouped by `skill`, case-insensitive).
   - **Shared cache prefix (don't break it):** both calls send byte-identical `tools` (`tailor.shared_tools()`: `submit_cv`, then `submit_gap_report` and `web_search` with `defer_loading: true`), `betas` (`tailor.BETAS`, incl. `mid-conversation-tool-changes-2026-07-01`) and the first two system blocks (`SHARED_PROMPT`, then the profile with `cache_control`). Each task's prompt is the third system block, after the breakpoint. The gap call appends a `role: "system"` message with `tool_removal` (submit_cv) and `tool_addition` (submit_gap_report, web_search). `tests/test_tailor.py::test_gap_call_shares_the_cached_prefix` guards this.
   - `submit_gap_report` is not `strict`: two strict schemas in one request exceed the API's grammar limit; pydantic + the repair loop catch shape errors. `submit_cv` stays strict.
   - Web search (`GAP_WEB_SEARCH`, default off, opt-in with `1`): `web_search_20260209` with `allowed_callers: ["direct"]` (otherwise it can't be surfaced from deferred) and `max_uses` = `GAP_MAX_SEARCHES` (default 3); the prompt says to search only for new recommendations for partial/missing must-haves, and not at all when none are left. `pause_turn` is resumed by resending. A recommendation's `url` is kept only if that exact (normalised) URL appears in the search results/citations of the conversation; otherwise `url` is null and `search_url` is a Google search for "<title> <provider>". Searches cost $10/1,000 (in `cvgen/costs.py`). With it off, the tool isn't declared at all.
   - Recommendations cache: `data/cache/recommendations_cache.json` (`gaps.RECOMMENDATIONS_CACHE`; not `data/*.json`, which `generate.py` renders), keyed by `skill`, at most 3 non-project recommendations per skill, entries older than 90 days ignored. The prompt lists saved ones with ids like `aws-1`; Claude reuses one with `{"requirement_ids": [...], "cached": "<id>"}` instead of searching again, and `expand_cached()` fills it in. Tests point it at a tmp file (`tests/conftest.py`); `cvgen bench` gives every bench its own fresh cache under `output/bench/`.
   - The report is written to the candidate in the second person.
10. Browser UI: `cvgen/server.py` (FastAPI + uvicorn, 127.0.0.1 only, port `CVGEN_PORT` or 8765; `cvgen serve` / `python -m cvgen.server [--log-file]`), `ui/CV Generator.html` (one self-contained file, inline CSS/JS, no CDN, QuizForge's design tokens) and `cvgen/service.py` (`cvgen service install|uninstall|start|stop|status`; `start`/`stop` act on the running server now and leave autostart alone).
   - API: `POST /api/jobs` {jd, company?, role?, pages?, gaps?} queues a `run()` (one worker thread, jobs one at a time, persisted to `output/.jobs.json`; jobs interrupted by a restart become failed); `GET /api/jobs` (today's), `GET /api/jobs/{id}` (stage from `stage_for(progress message)`: queued, tailoring, validating, rendering, layout round n/m, gap report, done, failed; when done: folder, `/files/...` links, layout, `summary`, gap report JSON), `POST /api/jobs/{id}/open-folder`, `POST /api/shutdown`, `GET /api/health`, `GET /files/<folder>/<file>` (output/ only, no dotfiles, no traversal), `GET /` (the page with port + token injected).
   - Security (don't weaken): Host must be 127.0.0.1/localhost:<port> (DNS rebinding); a present Origin must be "null" or http://127.0.0.1|localhost:<port>, and POSTs must have one; every /api route except health needs the `X-CVGen-Token` header = `.cvgen_token` (git-ignored, created on first use), because sandboxed iframes on any site also send Origin "null". CORS headers only on /api, never on `/` or /files (so no other origin can read the token or the files). Errors pass through `scrub()` so the API key can't appear in responses; the committed HTML has `"token": ""`.
   - The page's config line is `/*CVGEN-CONFIG*/{...}/*END-CVGEN-CONFIG*/`; `page_html()` fills it for the served page and the desktop copy. All job data is inserted with textContent.
   - Autostart: Windows uses a Startup-folder `.lnk` to `pythonw.exe -m cvgen.server --log-file logs/server.log` (no admin rights; Task Scheduler logon tasks usually need them), created via PowerShell's WScript.Shell; macOS a LaunchAgent (`com.cvgen.server`, RunAtLoad); Linux a systemd user unit (`cvgen-server.service`). Install also starts the server now and copies the page to the desktop (Windows desktop path from PowerShell, so OneDrive redirection works); uninstall stops it via `/api/shutdown` and removes only a desktop page containing the config marker.
   - The server sets `cvgen.render.SUBPROCESS_FLAGS = CREATE_NO_WINDOW` on Windows so LibreOffice doesn't flash a console under pythonw; its default (0) leaves `generate.py` unchanged.
11. Costs: `cvgen/costs.py` holds the price table (from the live pricing page; the check date is in `PRICES_CHECKED`; update both together, never from memory): input, 5-minute and 1-hour cache writes, cache reads, output per model, Haiku 5.5's over-100K-prompt rates, and web search at $10/1,000. Every API call becomes a row via `costs.call_row(step, message, effort)`: step (`tailor`, `repair`, `layout round N`, `gap report`, `gap repair`), model (the one that actually answered), effort, input / 5m and 1h cache-write / cache-read / output tokens, web searches, `cost_usd`. Rows are in `TailorResult.usage["calls"]`, `meta["layout"]["usage"]["calls"]` and `GapResult.usage["calls"]`; failures after a paid call carry theirs in `exc.calls`. `run()` writes them all to `cv.meta.json["calls"]` with `cost_usd` and `cost_by_step` (tailor/repair/layout/gap_report), and appends one line per CV (including drafts) to `<output root>/costs.csv`. `cvgen costs` averages that CSV over 7 and 30 days; `cvgen bench [dir]` runs every JD in `samples/benchmark/` into `output/bench/` (so it doesn't touch the real costs.csv or skills backlog) and prints a per-step table sorted by cost plus the top 3 cost drivers (step x component).
12. Cost settings: `cvgen/settings.py`, read from the environment / `.env` at request time (never at import). Defaults are what `cvgen bench` showed to be cheapest at the same CV quality; compare runs in `output/bench_compare/<label>/` (PDFs, `cv.json`, gap reports, `bench.json`) before changing one.

    | Setting | Default | Notes |
    |---|---|---|
    | `TAILOR_MODEL` (`CV_MODEL`) | `claude-opus-5-5` | the CV itself. Sonnet 5.5 was ~$0.10/CV cheaper but wrote thinner CVs (fewer bullets and skills); keep Opus |
    | `TAILOR_EFFORT` (`CV_EFFORT`) | `medium` | `high` cost ~45% more for an equivalent CV; `low` triggered repairs that ate its savings |
    | `GAP_MODEL` | `claude-opus-5-5` | Sonnet 5.5 judged gaps far more leniently (fewer must-have gaps, higher fit) |
    | `GAP_EFFORT` | `medium` | as strict as `high` on must-haves, ~20% cheaper |
    | `GAP_MAX_SEARCHES` | `3` | web searches per gap report (`max_uses`), only with `GAP_WEB_SEARCH=1` |
    | `GAP_WEB_SEARCH` | `0` | opt-in. Off, the tool isn't declared and links are Google search links; on, it about doubles the gap report's cost (search results are billed as input and cached) |
    | `LAYOUT_MODEL` | `claude-haiku-5-5` | widow rewrites only; overflow is trimmed in Python |
    | `LAYOUT_EFFORT` | `medium` | |
    | `CACHE_TTL` | `5m` | `5m` or `1h` for the cached profile prefix. 1h writes cost 1.6x but pay off only when CVs are 5-60 minutes apart; within a batch, 5m already hits |
8. `cvgen/cli.py`: the `cvgen` command (`[project.scripts]` in `pyproject.toml`; `pip install -e .`; also `python -m cvgen`). `cvgen new` (paste JD or `--jd-file`; `--company --role --pages 1|2 --no-layout-fix --no-gaps --model --profile`), `cvgen batch <dir>` (every `.txt`, setup checked once up front, one failure doesn't stop the rest, summary table with company/role/folder/fit/pages/widows left/cost/status, exit 1 if any failed), `cvgen rerender <folder>`, `cvgen profile check [path]`. `-v` enables INFO logs (token usage per call). `explain()` turns API errors (401/403/404/429/529/5xx/timeouts/connection) and pipeline errors into one clear message. The old `python -m cvgen.cli jd.txt` form still works (it becomes `new --jd-file`).

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
cvgen/costs.py       price table, per-call cost rows, output/costs.csv, `cvgen costs` / `cvgen bench` reports
cvgen/gaps.py        skill-gap report (gap_report.json/.md) and output/skills_backlog.md
cvgen/pipeline.py    run(...) -> output folder; rerender(folder); setup checks
cvgen/cli.py         the `cvgen` command (new, batch, rerender, profile check, serve, service)
cvgen/server.py      local API + page server (127.0.0.1, token + Origin checks, job queue)
cvgen/service.py     autostart at login per OS, desktop copy of the page
ui/CV Generator.html the browser page (self-contained)
cvgen/__main__.py    `python -m cvgen`
pyproject.toml       package + `cvgen` entry point (packages = ["cvgen"] only)
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
| `availability` | string | optional; its own centred line under the headline (relocation, remote), shown only if present |
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

Measured on the template: the usable width is 7.5in, text is Calibri 11pt, and a full bullet line holds 102–122 characters (median 113) in the LibreOffice PDF. `CHARS_PER_LINE = 110`. Bullets max 2 lines (220 chars), 6 per role, 4 per project, 3 projects; headline 1 line (110); availability 1 line (110, separate from the headline); summary 5 lines (550); 9 skill categories, 60 skills, 2 lines per skill category; `MAX_PAGES = 2`; layout: `WIDOW_MAX_WORDS = 4`, `MAX_LAYOUT_ROUNDS = 2`. Avoid aiming for 115–135-character bullets: they wrap onto a second line holding a word or two. Change limits only in `limits.py`.

## Profile (`profile.json`)

Schema: `cvgen/profile_schema.py`; example: `profile.example.json`. A superset of the CV: `contact`, `headlines` and `summaries` (variants), `experience` (company, title, location, `start`/`end` as `YYYY-MM`, `end: null` = Present, bullets), `projects`, `education`, `certifications` (`date` or `status`), `skills` (groups of named skills), `languages`, `preferences`. Every item has a stable `id` (lowercase-hyphenated, e.g. `exp-acme-03`) and optional `tags`.

- `profile.json` was compiled from all 27 CV JSONs under `data/` (including `data/old/`). Bullets describing the same achievement are merged into one: `text` is the strongest original wording, `facts` holds extra numbers/tech/scope found only in other wordings (each a verbatim excerpt), `angles` says what it demonstrates, `sources` lists the CV files it came from (paths relative to `data/`), or `user-input:<YYYY-MM-DD>` for content the user supplied directly. A project may also have `compact_bullets`: a shorter alternative set where each bullet can summarise several regular ones, so a CV uses `bullets` or `compact_bullets` for a project, never both. Internship entries carry `positions`, and their bullets a `position`. Summaries and headlines have a `target_role`.
- Tailoring may use a bullet's `text` and its `facts`, nothing else. `preferences` (`target_roles`, `never_claim`, `tone`, `availability`) is filled in by hand and is never a source of facts, except `availability`: id'd lines like "Willing to relocate to Bucharest" that the candidate states, copied verbatim by id; anything in `never_claim` must not appear in a CV.
- `python -m cvgen.profile check profile.json`: errors (exit 1) for invalid schema, missing ids and duplicate ids; warnings for bullets with no metric (number, %, "two", "hundreds", ...).
- `load_profile()` is what the pipeline uses; it refuses a profile with errors.
- Ids are permanent: renaming one breaks traceability of CVs generated earlier. Add new ids instead of reusing old ones.
- `python -m cvgen.convert` builds a starter profile from `data/*.json` and `data/old/*.json` (newest wins) and never overwrites an existing file. `title`, `location`, `workModel` and `contractor` appear in older JSONs for `templates/cv_template.docx` and are ignored by the current template.

## Environment

- Python 3.11, `pip install -r requirements.txt`; LibreOffice installed.
- `ANTHROPIC_API_KEY` in `.env` (see `.env.example`), loaded only by the AI pipeline. `.env` and `output/` are git-ignored.
