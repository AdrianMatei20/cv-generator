"""Command-line entry point for the AI pipeline: JD in, output/<Company>_<Role>_<YYYY-MM-DD>/ out.

    python -m cvgen.cli samples/jd_example.txt
    python -m cvgen.cli jd.txt --company Google --role "Software Engineer"
    python -m cvgen.cli - < jd.txt
"""

import argparse
import logging
import sys
from pathlib import Path

from cvgen.pipeline import DEFAULT_PROFILE, PipelineError, run
from cvgen.tailor import TailorError


def main(argv=None) -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m cvgen.cli", description="Tailor the CV to a job description.")
    parser.add_argument("jd", help="job description text file, or - for stdin")
    parser.add_argument("--company", help="company name for the output folder (overrides the one Claude reads from the JD)")
    parser.add_argument("--role", help="role for the output folder (overrides the one Claude reads from the JD)")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="master profile (default: profile.json)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    jd = sys.stdin.read() if args.jd == "-" else Path(args.jd).read_text(encoding="utf-8")
    try:
        folder = run(jd, args.company, args.role, profile_path=args.profile)
    except (PipelineError, TailorError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(folder)
    print_layout_report(folder)
    return 0


def print_layout_report(folder: Path) -> None:
    import json

    meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
    lay = meta.get("layout", {})
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


if __name__ == "__main__":
    sys.exit(main())
