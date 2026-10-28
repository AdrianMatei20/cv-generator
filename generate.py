import os
import json
from cvgen.render import render, anonymize

# ----------------------
# CONFIG
# ----------------------
TEMPLATE_PATH = "templates/cv_template_with_summary.docx"
#TEMPLATE_PATH = "templates/cv_template_with_summary.docx"
#TEMPLATE_PATH = "templates/cv_template_good_bullet_points.docx"
DATA_DIR = "data"
OUTPUT_DIR = "output"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------
# LOAD JSON
# ---------------------------
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------------------------
# PROCESS SINGLE FILE
# ---------------------------
def process_file(file_path):
    base = os.path.splitext(os.path.basename(file_path))[0]
    print(f"Processing: {base}")

    data = load_json(file_path)

    # Create folder per CV
    person_name = data.get("name", "Candidate").replace(" ", "")
    folder_path = os.path.join(OUTPUT_DIR, base)
    os.makedirs(folder_path, exist_ok=True)

    # NORMAL CV
    render(data, folder_path, f"CV_{person_name}", TEMPLATE_PATH)

    # ANONYMIZED CV
    render(anonymize(data), folder_path, "CV_JohnDoe", TEMPLATE_PATH)

    print(f"Done: {base}")

# ---------------------------
# MAIN
# ---------------------------
def main():
    files = [f for f in os.listdir(DATA_DIR) if f.endswith(".json")]

    if not files:
        print("No JSON files found in /data")
        return

    for file in files:
        process_file(os.path.join(DATA_DIR, file))


if __name__ == "__main__":
    main()
