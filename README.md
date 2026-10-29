# CV Generator

Generates a CV as `.docx` and `.pdf` from a Word template, in two ways:

- **Manual:** you write the CV as JSON and run `python generate.py`. No AI, no API key.
- **Tailored:** you give `cvgen` a job description, and Claude builds a CV for it from your master profile (`profile.json`), using only facts that are in the profile.

## Setup

1. **Python 3.10+** (developed on 3.11).
2. **LibreOffice**, used to convert the `.docx` to PDF: install it from https://www.libreoffice.org/download/download-libreoffice/ and put its program folder (`C:\Program Files\LibreOffice\program\` on Windows) on `PATH`. The default Windows install location is found even if it isn't on `PATH`.
3. **Dependencies:**
   - Manual flow only: `pip install docxtpl`
   - Everything, plus the `cvgen` command: `pip install -e .` (or `pip install -r requirements.txt` and use `python -m cvgen`)
4. **API key** (tailored flow only): copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. Optional settings there: `CV_MODEL` (default `claude-opus-5-5`), `CV_EFFORT` (default `high`), `CANDIDATE_FILENAME` (default `AdrianMatei`, giving `CV_AdrianMatei.pdf`).
5. **Profile** (tailored flow only): `profile.json` holds everything about you; `profile.example.json` shows the format. Check it with `cvgen profile check`.

## Manual flow (unchanged, no AI)

Write or edit a CV JSON in `data/`, then:

```
python generate.py
```

Every `data/*.json` (top level only) is rendered with `templates/cv_template_with_summary.docx` into `output/<json name>/CV_<YourName>.docx/.pdf`, plus an anonymised `CV_JohnDoe.docx/.pdf`. This needs only `docxtpl` and LibreOffice, and works without an API key or any of the AI packages installed.

## Tailored flow

`cvgen` sends your profile and the job description to Claude, which picks the most relevant jobs, projects, skills and bullets, rewords them towards the JD's keywords, and cites the profile ids each piece came from. It can't change titles, employers or dates: those are copied from the profile by id. The result is validated in Python (schema, length limits, every source id real, every number present in its sources, no first person); if anything fails, the errors go back to Claude to fix, up to twice. The CV is then rendered with the same template and LibreOffice conversion as the manual flow, and the PDF is checked for widows (a wrapped bullet whose last line has 4 words or fewer) and for going over the page limit; Claude rewrites just those bullets, or drops the least relevant ones, for up to two rounds, and the best version is kept. Requirements in the JD that your profile has no evidence for are listed as gaps instead of being claimed.

### Commands

```
cvgen new                                   # paste the JD, then Ctrl+Z Enter (Windows) or Ctrl+D (macOS/Linux)
cvgen new --jd-file jd.txt                  # read the JD from a file
cvgen new --jd-file jd.txt --company Google --role "Software Engineer"   # override the folder name parts
cvgen new --jd-file jd.txt --pages 1        # one-page CV
cvgen new --jd-file jd.txt --no-layout-fix  # render and report widows, but don't rewrite anything
cvgen new --jd-file jd.txt --model claude-sonnet-5-5

cvgen batch jds/                            # every .txt in jds/, one after another, then a summary table
cvgen rerender output/Google_SoftwareEngineer_2026-10-26    # after editing that folder's cv.json by hand
cvgen profile check                         # validate profile.json (ids, schema, bullets without metrics)
```

Add `-v` before the command (`cvgen -v new ...`) for detailed logs, including token usage per API call. Each run prints its progress, the layout rounds, any gaps, and an estimated cost (typically $0.25-0.35 with Opus 5.5; repeated runs within 5 minutes reuse the cached profile and cost less).

### Output

Each run writes a new folder, `output/<Company>_<Role>_<YYYY-MM-DD>/` (with `_2`, `_3`, ... if it already exists):

| File | What it is |
|---|---|
| `CV_AdrianMatei.pdf`, `CV_AdrianMatei.docx` | the CV |
| `cv.json` | exactly what was rendered; edit it and run `cvgen rerender` to re-render in place |
| `cv.meta.json` | profile ids behind every line, gaps, repairs, layout rounds, token usage, cost, model |
| `job_description.txt` | the JD as given |

If the CV still fails validation after the repairs, the folder gets `cv.draft.json` instead: fix it, save it as `cv.json`, and run `cvgen rerender` on the folder.

## Tests

```
pytest
```

Everything runs offline (Claude and LibreOffice are faked where needed), except `tests/test_legacy.py`, which runs the real `python generate.py` with LibreOffice and compares the output with `tests/golden/`.
