"""Render a CV dict to .docx (docxtpl) and .pdf (LibreOffice).

Shared by the legacy `python generate.py` command and the AI pipeline.
Must not import anything from the AI path (anthropic, pydantic, pymupdf, dotenv).
"""

import os
import json
import subprocess
import shutil
from pathlib import Path
from docxtpl import DocxTemplate, RichText

# ----------------------
# CONFIG
# ----------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = REPO_ROOT / "templates" / "cv_template_with_summary.docx"

LIBREOFFICE_FALLBACK = r"C:\Program Files\LibreOffice\program\soffice.exe"

# creationflags for the LibreOffice subprocess. 0 (the default) behaves exactly as before;
# the background server sets CREATE_NO_WINDOW on Windows so no console window flashes.
SUBPROCESS_FLAGS = 0

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

# ---------------------------
# DOCX GENERATION
# ---------------------------
def generate_docx(data, output_path, template_path=DEFAULT_TEMPLATE):
    doc = DocxTemplate(template_path)

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

def convert_to_pdf(docx_path, output_dir):
    soffice = get_soffice_path()

    subprocess.run([
        soffice,
        "--headless",
        "--convert-to", "pdf",
        str(docx_path),
        "--outdir", str(output_dir)
    ], creationflags=SUBPROCESS_FLAGS)

# ---------------------------
# RENDER
# ---------------------------
def render(cv, out_dir, basename, template=DEFAULT_TEMPLATE):
    """Write <out_dir>/<basename>.docx and .pdf from a CV dict; return both paths.

    `cv` is left untouched: ampersands are escaped on a copy, and the copy
    is what gets hyperlink RichText objects added to it.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = escape_ampersands(cv)
    docx_path = out_dir / f"{basename}.docx"

    generate_docx(data, str(docx_path), str(template))
    convert_to_pdf(docx_path, out_dir)

    return docx_path, out_dir / f"{basename}.pdf"
