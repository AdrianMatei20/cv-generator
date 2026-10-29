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
from datetime import datetime
from pathlib import Path

import anthropic

from cvgen import costs, settings
from cvgen.pipeline import (ANON_BASENAME, DEFAULT_OUTPUT_ROOT, DEFAULT_PROFILE, MAX_RETRIES, PipelineError, SetupError, check_api_key,
                            check_libreoffice, load_profile_checked, rerender, run)
from cvgen.tailor import TailorError

COMMANDS = {"new", "batch", "rerender", "profile", "serve", "service", "costs", "bench", "-h", "--help"}


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


def _pdf(folder: Path, anonymized: bool = False) -> Path | None:
    return next((p for p in sorted(folder.glob("CV_*.pdf")) if (p.stem == ANON_BASENAME) == anonymized), None)


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
    steps = meta.get("cost_by_step") or {}
    detail = ", ".join(f"{name.replace('_', ' ')} {costs.fmt(usd)}" for name, usd in steps.items() if usd)
    print(f"Estimated cost: {costs.fmt(meta.get('cost_usd'))} ({meta.get('model')})" + (f": {detail}" if detail else ""))
    print(f"\nFolder: {folder}")
    print(f"PDF:    {_pdf(folder)}")
    if _pdf(folder, anonymized=True):
        print(f"        {_pdf(folder, anonymized=True)} (anonymized)")
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
                     str(lay.get("final_pages", "")), str(lay.get("final_widows", "")), costs.fmt(run_cost), "ok"])
        print(f"  done: {rel} ({costs.fmt(run_cost)})")

    print(f"\nSummary: {len(files) - failures} succeeded, {failures} failed, estimated total {costs.fmt(total)}\n")
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


def cmd_serve(args) -> int:
    from cvgen.server import main as serve
    return serve(["--port", str(args.port)] if args.port else [])


def cmd_service(args) -> int:
    from cvgen import service
    lines = {"install": service.install, "uninstall": service.uninstall, "start": service.start,
             "stop": service.stop_server, "status": service.status}[args.action]()
    for line in lines:
        print(line)
    return 0


def _num(n) -> str:
    return f"{int(n):,}"


def cmd_costs(args) -> int:
    lines = costs.read_csv(args.csv)
    if not lines:
        print(f"No costs recorded yet in {args.csv}. Every CV run adds a line.")
        return 0
    rows = []
    for days in (7, 30):
        r = costs.report(lines, days)
        rows.append([f"last {days} days", str(r["cvs"]), costs.fmt(r["total"]), costs.fmt(r["per_cv"]),
                     *(costs.fmt(r["per_step"][b]) for b in costs.BUCKETS)])
    print(f"Average cost per CV, from {args.csv} ({len(lines)} CV(s) recorded)\n")
    print_table(rows, ["Period", "CVs", "Total", "Per CV", "Tailor", "Repair", "Layout", "Gap report"])
    return 0


COMPARE_FILES = ("CV_*.pdf", "cv.json", "gap_report.md", "gap_report.json")


def copy_for_compare(folder: Path, target: Path) -> None:
    import shutil
    target.mkdir(parents=True, exist_ok=True)
    for pattern in COMPARE_FILES:
        for f in folder.glob(pattern):
            shutil.copy2(f, target / f.name)


def cmd_bench(args) -> int:
    files = sorted(Path(args.folder).glob("*.txt"))
    if not files:
        raise SetupError(f"No .txt files in {args.folder}")
    load_profile_checked(args.profile)
    check_api_key()
    check_libreoffice()
    root = DEFAULT_OUTPUT_ROOT / "bench"  # kept apart from your real output, costs.csv and skills backlog
    compare = DEFAULT_OUTPUT_ROOT / "bench_compare" / args.label if args.label else None
    if compare and compare.exists():
        raise SetupError(f"{compare} already exists; pick another --label")
    from cvgen import gaps
    gaps.RECOMMENDATIONS_CACHE = root / f"recommendations_cache_{datetime.now():%Y%m%d_%H%M%S}.json"  # fresh per bench
    rows, per_cv = [], []
    for i, path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {path.name}")
        try:
            folder = run(path.read_text(encoding="utf-8"), progress=Progress(), output_root=root,
                         profile_path=args.profile, model=args.model)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"  FAILED: {explain(exc)}")
            continue
        meta = _meta(folder)
        rows += [{**r, "jd": path.name} for r in meta.get("calls", [])]
        if compare:
            copy_for_compare(folder, compare / path.stem)
        per_cv.append((path.name, meta.get("company", ""), meta.get("cost_usd")))
    if not rows:
        print("\nNo successful runs.")
        return 1

    print(f"\nBenchmark: {len(per_cv)} CV(s) from {args.folder}, model {rows[0]['model']}, "
          f"prices checked {costs.PRICES_CHECKED}\n")
    table = costs.bench_table(rows)
    total = costs.totals(rows)

    def line(name, t):
        return [name, str(t["calls"]), _num(t["input_tokens"]), _num(t["cache_write_5m_tokens"] + t["cache_write_1h_tokens"]),
                _num(t["cache_read_tokens"]), _num(t["output_tokens"]), str(t["web_searches"]), costs.fmt(t["cost_usd"])]
    print_table([line(t["step"], t) for t in table] + [line("TOTAL", total)],
                ["Step", "Calls", "Input", "Cache write", "Cached", "Output", "Searches", "Cost"])
    print("\nPer CV:")
    for name, company, usd in per_cv:
        print(f"  {name:45} {company:28} {costs.fmt(usd)}")
    known = [usd for _, _, usd in per_cv if usd is not None]
    if known:
        print(f"  {'average':45} {'':28} {costs.fmt(sum(known) / len(known))}")
    print("\nTop cost drivers:")
    for n, (step, part, usd) in enumerate(costs.cost_drivers(rows), 1):
        share = usd / total["cost_usd"] * 100 if total["cost_usd"] else 0
        print(f"  {n}. {step}: {part}, {costs.fmt(usd)} ({share:.0f}% of the total)")
    report = (compare or root) / ("bench.json" if compare else f"bench_{datetime.now():%Y-%m-%d_%H%M%S}.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    settings_used = {k: settings.get(k) for k in settings.DEFAULTS}
    report.write_text(json.dumps({"label": args.label, "settings": settings_used, "files": [f.name for f in files],
                                  "calls": rows, "per_cv": per_cv}, indent=1), encoding="utf-8")
    print(f"\nRaw rows: {report}")
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

    serve = sub.add_parser("serve", help="run the local server for the CV Generator page (foreground)")
    serve.add_argument("--port", type=int, help="default: CVGEN_PORT or 8765")
    serve.set_defaults(func=cmd_serve)

    cst = sub.add_parser("costs", help="average cost per CV and per step over the last 7 and 30 days")
    cst.add_argument("--csv", type=Path, default=DEFAULT_OUTPUT_ROOT / "costs.csv")
    cst.set_defaults(func=cmd_costs)

    bench = sub.add_parser("bench", help="run every JD in samples/benchmark/ and print the cost per step (real API calls)")
    bench.add_argument("folder", nargs="?", default=str(Path(__file__).resolve().parent.parent / "samples" / "benchmark"))
    bench.add_argument("--label", help="also copy each CV's PDFs, cv.json and gap report to output/bench_compare/<label>/")
    bench.add_argument("--model", help="tailor model (default: TAILOR_MODEL)")
    bench.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    bench.set_defaults(func=cmd_bench)

    svc = sub.add_parser("service", help="run the server in the background (install/uninstall at login, start/stop now, status)")
    svc.add_argument("action", choices=("install", "uninstall", "start", "stop", "status"))
    svc.set_defaults(func=cmd_service)

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
