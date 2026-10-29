"""Load and check the master profile (profile.json): the only source of facts about the candidate.

    python -m cvgen.profile check profile.json

Errors (exit code 1): invalid JSON or schema, missing ids, duplicate ids.
Warnings (exit code 0): bullets with no metric. Not every bullet needs one,
but the list shows where a number could make a bullet stronger.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from cvgen.profile_schema import Bullet, Profile

# A standalone number (40%, 3x, 1,200, 11, 2.5k) or a word that implies one.
# Heuristic: version-like numbers such as "OAuth 2.0" also count.
_METRIC = re.compile(
    r"(?<![\w.])\d+(?:[.,]\d+)*\s*(?:%|x|×|\+|k|m|ms|s|h)?(?![\w.])"
    r"|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty"
    r"|hundreds|thousands|millions|dozens|doubled|tripled|halved)\b",
    re.IGNORECASE,
)


class ProfileError(ValueError):
    pass


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    profile: Profile | None = None


def has_metric(text: str) -> bool:
    return bool(_METRIC.search(text))


def check_profile(data: dict) -> CheckResult:
    result = CheckResult()
    try:
        profile = Profile.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "(root)"
            result.errors.append(f"{loc}: {err['msg']}")
        return result
    result.profile = profile

    seen: dict[str, list[str]] = {}
    for path, item in profile.items():
        if item.id is None:
            result.errors.append(f"{path}: missing id")
        else:
            seen.setdefault(item.id, []).append(path)
        if isinstance(item, Bullet) and not has_metric(item.text):
            result.warnings.append(f"{item.id or path}: no metric: {item.text}")
    for item_id, paths in seen.items():
        if len(paths) > 1:
            result.errors.append(f"duplicate id '{item_id}': {', '.join(paths)}")
    return result


def read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_profile(path: Path) -> Profile:
    """Load a profile for the pipeline; raise ProfileError if the check finds errors."""
    result = check_profile(read_json(Path(path)))
    if result.errors:
        raise ProfileError(f"{path} has {len(result.errors)} error(s):\n" + "\n".join(result.errors))
    return result.profile


def _check_command(path: Path) -> int:
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR  {path}: {exc}")
        return 1
    result = check_profile(data)
    for line in result.errors:
        print(f"ERROR  {line}")
    for line in result.warnings:
        print(f"WARN   {line}")
    if result.profile is not None:
        counts = {}
        for _, item in result.profile.items():
            kind = type(item).__name__
            counts[kind] = counts.get(kind, 0) + 1
        print("Items: " + ", ".join(f"{n} {kind}" for kind, n in counts.items()))
    status = "FAILED" if result.errors else "OK"
    print(f"{status}: {path}: {len(result.errors)} error(s), {len(result.warnings)} warning(s)")
    return 1 if result.errors else 0


def main(argv=None) -> int:
    # Bullets may hold characters (→, ●) the Windows console code page can't print.
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m cvgen.profile")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate a profile.json")
    check.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    return _check_command(args.path)


if __name__ == "__main__":
    sys.exit(main())
