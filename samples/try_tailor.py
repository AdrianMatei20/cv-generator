"""Run tailor() on a sample JD and print the CV JSON and token usage. Calls the Claude API.

    python samples/try_tailor.py [jd.txt] [profile.json]

Run it twice within 5 minutes: the second run should show cache_read > 0.
Also writes cv.json and cv.meta.json to output/tailor-try/ for inspection.
"""

import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

from cvgen.tailor import tailor  # noqa: E402

jd_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "samples" / "jd_example.txt"
profile_path = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / "profile.json"

load_dotenv(REPO / ".env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
sys.stdout.reconfigure(encoding="utf-8")

profile = json.loads(profile_path.read_text(encoding="utf-8"))
result = tailor(profile, jd_path.read_text(encoding="utf-8"))

print(json.dumps(result.cv, ensure_ascii=False, indent=2))
print("\n--- meta ---")
print(json.dumps(result.meta(), ensure_ascii=False, indent=2))

out = REPO / "output" / "tailor-try"
out.mkdir(parents=True, exist_ok=True)
(out / "cv.json").write_text(json.dumps(result.cv, ensure_ascii=False, indent=2), encoding="utf-8")
(out / "cv.meta.json").write_text(json.dumps(result.meta(), ensure_ascii=False, indent=2), encoding="utf-8")

u = result.usage
print(f"\nusage: input={u['input_tokens']} output={u['output_tokens']} "
      f"cache_write={u['cache_creation_input_tokens']} cache_read={u['cache_read_input_tokens']}")
