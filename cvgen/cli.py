"""The `cvgen` command (installed by `pip install -e .`; also `python -m cvgen`).

    cvgen new                         paste a JD, then Ctrl+Z Enter (Windows) / Ctrl+D (macOS, Linux)
    cvgen new --jd-file jd.txt --company Google --role "Software Engineer" --pages 1
    cvgen batch jds/                  every .txt in the folder, then a summary table
    cvgen rerender output/<folder>    after editing that folder's cv.json by hand
    cvgen profile check [profile.json]
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import anthropic

from cvgen import cost
from cvgen.pipeline import (DEFAULT_OUTPUT_ROOT, DEFAULT_PROFILE, MAX_RETRIES, PipelineError, SetupError, check_api_key,
                            check_libreoffice, load_profile_checked, rerender, run)
from cvgen.tailor import TailorError

COMMANDS = {"new", "batch", "rerender", "profile", "-h", "--help"}


# ---------------------------
# OUTPUT HELPERS
# ---------------------------
class Progress:
    """Prints '  [0:42] message' lines, optionally prefixed (batch)."""

    def __init__(self, prefix: str = ""):
        self.start = time.monotonic()
        self.prefix = prefix

    def __call__(self, message: str) -> None:
        elapsed = int(time.monotonic() - self.start)
        print(f"  [{elapsed // 60}:{elapsed % 60:02d}] {self.prefix}{message}", flush=True)


def explain(exc: BaseException) -> str:
    """A clear one-paragraph message for anything a run can fail with."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "The API key was rejected (401). Check ANTHROPIC_API_KEY in .env."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"The API key isn't allowed to use this model (403): {exc.message}"
    if isinstance(exc, anthropic.NotFoundError):
        return f"Model or endpoint not found (404). Check --model / CV_MODEL. {exc.message}"
    if isinstance(exc, anthropic.RateLimitError):
        return f"Rate limited by the API even after {MAX_RETRIES} retries with backoff. Wait a minute and run it again."
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 529:
            return f"The API is overloaded (529) even after {MAX_RETRIES} retries with backoff. Try again in a few minutes."
        if exc.status_code >= 500:
            return f"API server error ({exc.status_code}) after {MAX_RETRIES} retries. Try again later."
        return f"API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "The API request timed out after retries. Check your connection and try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API. Check your internet connection or proxy."
    if isinstance(exc, (PipelineError, TailorError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _meta(folder: Path) -> dict:
    return json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))


def _pdf(folder: Path) -> Path | None:
    return next(iter(sorted(folder.glob("CV_*.pdf"))), None)


def print_layout_report(folder: Path) -> None:
    lay = _meta(folder).get("layout", {})
    print("\nLayout")
    for r in lay.get("rounds", []):
        head = f"  round {r['round']}"
        if r["round"] == 0:
            print(f"{head}: {r['pages']} page(s), {len(r['widows'])} widow(s)"
                  + (f", {len(r['unmapped'])} bullet(s) not found in PDF" if r.get("unmapped") else ""))
        else:
            rewritten = [c for c in r.get("changes", []) if "after" in c]
            removed = [c for c in r.get("changes", []) if "removed" in c]
            stats = f": {r['pages']} page(s), {len(r['widows'])} widow(s)" if "pages" in r else ""
            print(f"{head}: {len(rewritten)} rewritten, {len(removed)} removed -> {r['result']}{stats}")
        for w in r.get("widows", []):
            print(f"      widow {w['path']}: last line \"{w['last_line']}\" ({w['last_line_words']} word(s))")
        for c in r.get("changes", []):
            if "after" in c:
                print(f"      rewrote {c['path']}:\n        - {c['before']}\n        + {c['after']}")
            elif "removed" in c:
                print(f"      removed {c['path']}: {c['removed']}")
        for e in r.get("validation_errors", []):
            print(f"      rejected: {e}")
    print(f"  kept round {lay.get('kept_round')}: final {lay.get('final_pages')} page(s), {lay.get('final_widows')} widow(s)")


def print_result(folder: Path) -> None:
    meta = _meta(folder)
    print_layout_report(folder)
    if meta.get("gaps"):
        print("\nGaps (JD asks for it, profile has no evidence; left out):")
        for gap in meta["gaps"]:
            print(f"  - {gap}")
    gap = meta.get("gap_report")
    if gap and "error" in gap:
        print(f"\nGap report failed: {gap['error'].splitlines()[0]}")
    elif gap:
        report = json.loads((folder / "gap_report.json").read_text(encoding="utf-8"))
        statuses = [r["status"] for r in report["requirements"]]
        print(f"\nFit: {report['fit']['score']}/100. {report['fit']['justification']}")
        print(f"  {statuses.count('have')} have, {statuses.count('partial')} partial, {statuses.count('missing')} missing; "
              f"{len(report['recommendations'])} recommendation(s); {gap['web_search']['requests']} web search(es)")
    u, lu, gu = meta.get("usage", {}), meta.get("layout", {}).get("usage", {}), (gap or {}).get("usage", {})
    print(f"\nTokens: tailoring {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out, "
          f"{u.get('cache_read_input_tokens', 0)} cache read, {u.get('cache_creation_input_tokens', 0)} cache write; "
          f"layout {lu.get('input_tokens', 0)} in / {lu.get('output_tokens', 0)} out"
          + (f"; gap report {gu.get('input_tokens', 0)} in / {gu.get('output_tokens', 0)} out, "
             f"{gu.get('cache_read_input_tokens', 0)} cache read" if gu else ""))
    print(f"Estimated cost: {cost.fmt(meta.get('cost_usd'))} ({meta.get('model')})")
    print(f"\nFolder: {folder}")
    print(f"PDF:    {_pdf(folder)}")
    if (folder / "gap_report.md").is_file():
        print(f"Gaps:   {folder / 'gap_report.md'}")
        print(f"        {DEFAULT_OUTPUT_ROOT / 'skills_backlog.md'}")


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()
    print(line(headers))
    print(line(["-" * w for w in widths]))
    for row in rows:
        print(line(row))


# ---------------------------
# COMMANDS
# ---------------------------
def _read_jd(args) -> str:
    if args.jd_file:
        return Path(args.jd_file).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        eof = "Ctrl+Z then Enter" if sys.platform == "win32" else "Ctrl+D"
        print(f"Paste the job description, then press {eof} on an empty line:", flush=True)
    return sys.stdin.read()


def _run_options(args) -> dict:
    return {"pages": args.pages, "layout_fix": not args.no_layout_fix, "gaps": not args.no_gaps, "model": args.model,
            "profile_path": args.profile}


def _fit(folder: Path) -> str:
    path = folder / "gap_report.json"
    if not path.is_file():
        return "-"
    return str(json.loads(path.read_text(encoding="utf-8"))["fit"]["score"])


def cmd_new(args) -> int:
    jd = _read_jd(args)
    print()
    folder = run(jd, args.company, args.role, progress=Progress(), **_run_options(args))
    print_result(folder)
    return 0


def cmd_batch(args) -> int:
    directory = Path(args.folder)
    files = sorted(directory.glob("*.txt"))
    if not files:
        raise SetupError(f"No .txt files in {directory}")
    # Fail once, up front, on problems that would fail every JD.
    load_profile_checked(args.profile)
    check_api_key()
    check_libreoffice()

    rows, failures, total = [], 0, 0.0
    for i, path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {path.name}")
        try:
            folder = run(path.read_text(encoding="utf-8"), progress=Progress(), **_run_options(args))
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # one bad JD must not stop the rest
            failures += 1
            message = explain(exc)
            print(f"  FAILED: {message}")
            rows.append([path.name, "", "", "", "", "", "", "", "FAILED: " + message.splitlines()[0][:70]])
            continue
        meta = _meta(folder)
        lay = meta.get("layout", {})
        run_cost = meta.get("cost_usd")
        total += run_cost or 0
        rel = folder.relative_to(DEFAULT_OUTPUT_ROOT.parent) if folder.is_relative_to(DEFAULT_OUTPUT_ROOT.parent) else folder
        rows.append([path.name, meta.get("company", ""), meta.get("role", ""), str(rel), _fit(folder),
                     str(lay.get("final_pages", "")), str(lay.get("final_widows", "")), cost.fmt(run_cost), "ok"])
        print(f"  done: {rel} ({cost.fmt(run_cost)})")

    print(f"\nSummary: {len(files) - failures} succeeded, {failures} failed, estimated total {cost.fmt(total)}\n")
    print_table(rows, ["JD", "Company", "Role", "Folder", "Fit", "Pages", "Widows left", "Cost", "Status"])
    if not args.no_gaps:
        print(f"\nSkills backlog: {DEFAULT_OUTPUT_ROOT / 'skills_backlog.md'}")
    return 1 if failures else 0


def cmd_rerender(args) -> int:
    result = rerender(Path(args.folder), profile_path=args.profile, progress=Progress())
    if result.validation_errors:
        print(f"\nWarning: the edited CV has {len(result.validation_errors)} validation issue(s) (rendered anyway):")
        for error in result.validation_errors:
            print(f"  - {error}")
    for w in result.report.widows:
        print(f"  widow {w.path}: last line \"{w.lines[-1].text.strip()}\" ({w.last_line_words} word(s))")
    over = f" (over the {result.report.pages}-page limit)" if result.report.over_pages else ""
    print(f"\n{result.report.pages} page(s){over}, {len(result.report.widows)} widow(s)")
    print(f"PDF: {result.pdf}")
    return 0


def cmd_profile(args) -> int:
    from cvgen.profile import _check_command
    return _check_command(Path(args.path))


# ---------------------------
# ENTRY POINT
# ---------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cvgen", description="Tailor your CV to a job description with Claude.")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detailed logs (token usage per call, warnings)")
    sub = parser.add_subparsers(dest="command", required=True)

    def run_flags(p):
        p.add_argument("--pages", type=int, choices=(1, 2), help="page limit for this run (default: limits.MAX_PAGES)")
        p.add_argument("--no-layout-fix", action="store_true", help="check the layout but don't ask Claude to fix it")
        p.add_argument("--no-gaps", action="store_true", help="skip the skill-gap report (gap_report.md, skills backlog)")
        p.add_argument("--model", help="Claude model (default: CV_MODEL or claude-opus-5-5)")
        p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="master profile (default: profile.json)")

    new = sub.add_parser("new", help="tailor the CV to one JD (pasted, or --jd-file)")
    new.add_argument("--jd-file", help="read the JD from this file instead of pasting it")
    new.add_argument("--company", help="company for the folder name (overrides the one read from the JD)")
    new.add_argument("--role", help="role for the folder name (overrides the one read from the JD)")
    run_flags(new)
    new.set_defaults(func=cmd_new)

    batch = sub.add_parser("batch", help="run every .txt JD in a folder and print a summary table")
    batch.add_argument("folder")
    run_flags(batch)
    batch.set_defaults(func=cmd_batch)

    rer = sub.add_parser("rerender", help="re-render an output folder from its hand-edited cv.json")
    rer.add_argument("folder")
    rer.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    rer.set_defaults(func=cmd_rerender)

    prof = sub.add_parser("profile", help="profile.json tools")
    prof_sub = prof.add_subparsers(dest="profile_command", required=True)
    check = prof_sub.add_parser("check", help="validate profile.json (ids, schema, metrics)")
    check.add_argument("path", nargs="?", default=str(DEFAULT_PROFILE))
    check.set_defaults(func=cmd_profile)
    return parser


def main(argv=None) -> int:
    sys.stdout.reconfigure(errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["new", "--jd-file", *argv]  # the older `python -m cvgen.cli jd.txt` form
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR,
                        format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (SetupError, PipelineError, TailorError, anthropic.APIError, OSError) as exc:
        print(f"\nError: {explain(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
