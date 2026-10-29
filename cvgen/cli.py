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
    return 0


if __name__ == "__main__":
    sys.exit(main())
