import os
import html
import json
import subprocess
import shutil
from docxtpl import DocxTemplate, RichText

# ----------------------
# CONFIG
# ----------------------
TEMPLATE_PATH = "templates/cv_template.docx"
#TEMPLATE_PATH = "templates/cv_template_with_summary.docx"
#TEMPLATE_PATH = "templates/cv_template_good_bullet_points.docx"
DATA_DIR = "data"
OUTPUT_DIR = "output"

LIBREOFFICE_FALLBACK = r"C:\Program Files\LibreOffice\program\soffice.exe"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------
# LOAD JSON
# ---------------------------
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------------------------
# ESCAPE SPECIAL CHARACTERS
# ---------------------------
def escape_ampersands(data):
    if isinstance(data, str):
        return data.replace('&', '&amp;')
    elif isinstance(data, dict):
        return {k: escape_ampersands(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [escape_ampersands(i) for i in data]
    return data

# ---------------------------
# ANONYMIZATION
# ---------------------------
def anonymize(data):
    d = json.loads(json.dumps(data))  # deep copy

    d["name"] = "John Doe"
    d["phone"] = "0000000000"
    d["email"] = "email@example.com"
    d["linkedinText"] = "linkedin.com"
    d["linkedinUrl"] = "https://www.linkedin.com/"
    d["githubText"] = "github.com"
    d["githubUrl"] = "https://github.com/"

    for job in d.get("experience", []):
        job["company"] = "Company Name"
    
    for p in d["projects"]:
        p["linkText"] = "github.com"
        p["linkUrl"] = "https://github.com/"

    for edu in d.get("education", []):
        edu["institution"] = "University Name"

    return d

# ---------------------------
# HYPERLINK SUPPORT
# ---------------------------
def prepare_links(doc, data):
    # print(data)
    # LinkedIn
    if "linkedinText" in data and "linkedinUrl" in data:
        rt = RichText()
        rt.add(
            data["linkedinText"],
            url_id=doc.build_url_id(data["linkedinUrl"]),
            color="0000FF",
            underline=True,
            font="Calibri",
            size=22
        )
        data["linkedin"] = rt
    # Github
    if "githubText" in data and "githubUrl" in data:
        rt = RichText()
        rt.add(
            data["githubText"],
            url_id=doc.build_url_id(data["githubUrl"]),
            color="0000FF",
            underline=True,
            font="Calibri",
            size=22
        )
        data["github"] = rt
    # Github Projects
    for p in data["projects"]:
        if "linkText" in p and "linkUrl" in p:
            rt = RichText()
            rt.add(
                p["linkText"],
                url_id=doc.build_url_id(p["linkUrl"]),
                color="0000FF",
                underline=True,
                font="Calibri",
                size=22
            )
            p["link"] = rt
    # print(data)

# ---------------------------
# DOCX GENERATION
# ---------------------------
def generate_docx(data, output_path):
    doc = DocxTemplate(TEMPLATE_PATH)

    prepare_links(doc, data)

    doc.render(data)
    doc.save(output_path)

# ---------------------------
# PDF CONVERSION
# ---------------------------
def get_soffice_path():
    path = shutil.which("soffice")
    if path:
        return path

    if os.path.exists(LIBREOFFICE_FALLBACK):
        return LIBREOFFICE_FALLBACK

    raise RuntimeError("LibreOffice not found. Install it or update path.")

def convert_to_pdf(docx_path):
    soffice = get_soffice_path()

    subprocess.run([
        soffice,
        "--headless",
        "--convert-to", "pdf",
        docx_path,
        "--outdir", OUTPUT_DIR
    ])

# ---------------------------
# PROCESS SINGLE FILE
# ---------------------------
def process_file(file_path):
    base = os.path.splitext(os.path.basename(file_path))[0]
    print(f"Processing: {base}")

    data = load_json(file_path)
    data = escape_ampersands(data)
    anon_data = anonymize(data)

    # NORMAL CV
    docx_path = os.path.join(OUTPUT_DIR, f"{base}.docx")
    generate_docx(data, docx_path)
    convert_to_pdf(docx_path)

    # ANONYMIZED CV
    anon_docx = os.path.join(OUTPUT_DIR, f"{base}_anon.docx")
    generate_docx(anon_data, anon_docx)
    convert_to_pdf(anon_docx)

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