"""Output folder naming and creation for the AI pipeline.

    output/<Company>_<Role>_<YYYY-MM-DD>/   e.g. output/Google_SoftwareEngineer_2026-10-26
    output/<Company>_<Role>_<YYYY-MM-DD>_2/ if that folder already exists
"""

import os
import re
import unicodedata
from datetime import date
from pathlib import Path

MAX_PART_CHARS = 40
DEFAULT_CANDIDATE_FILENAME = "AdrianMatei"
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_part(text: str | None, fallback: str = "Unknown") -> str:
    """'Senior Technical Support Engineer – L3' -> 'SeniorTechnicalSupportEngineerL3'.

    Strips accents, splits on anything that isn't a letter or digit (spaces, punctuation and
    the characters Windows/macOS forbid: \\ / : * ? " < > |), joins the words in PascalCase
    and cuts to MAX_PART_CHARS.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = re.split(r"[^\w]+|_", plain)
    name = "".join(w[:1].upper() + w[1:] for w in words if w)[:MAX_PART_CHARS]
    if not name or name.upper() in _WINDOWS_RESERVED:
        return fallback
    return name


def folder_name(company: str | None, role: str | None, day: date) -> str:
    return f"{sanitize_part(company)}_{sanitize_part(role)}_{day.isoformat()}"


def create_output_dir(root: Path, company: str | None, role: str | None, day: date | None = None) -> Path:
    """Create and return a new, empty output folder; never reuses an existing one."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = folder_name(company, role, day or date.today())
    for n in range(1, 1000):
        path = root / (base if n == 1 else f"{base}_{n}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"too many output folders named {base}_N in {root}")


def candidate_filename() -> str:
    """Base name for the CV files ('CV_<this>.pdf'), from CANDIDATE_FILENAME."""
    return sanitize_part(os.environ.get("CANDIDATE_FILENAME"), fallback=DEFAULT_CANDIDATE_FILENAME)
