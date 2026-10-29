import copy
import json
from pathlib import Path

import pytest
from docx import Document
from pydantic import ValidationError

from cvgen import limits
from cvgen.render import escape_ampersands, generate_docx
from cvgen.schema import CV, split_list

SAMPLE = Path(__file__).parent / "fixtures" / "data" / "sample.json"


@pytest.fixture
def cv_dict():
    """The legacy sample, trimmed to fit the limits."""
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["headline"] = "Backend Engineer – Node.js / TypeScript | Brasov, Romania"
    data["summary"] = data["summary"][: limits.MAX_SUMMARY_CHARS]
    data["skills"] = [
        line for line in data["skills"]
        if len(line["category"]) + 2 + len(line["skills"]) <= limits.MAX_SKILL_LINE_CHARS
    ][: limits.MAX_SKILL_CATEGORIES]
    for section, max_bullets in (("experience", limits.MAX_BULLETS_PER_ROLE), ("projects", limits.MAX_BULLETS_PER_PROJECT)):
        for item in data[section]:
            item["bullets"] = [b for b in item["bullets"] if len(b) <= limits.MAX_BULLET_CHARS][:max_bullets]
    return data


def test_valid_cv_passes(cv_dict):
    CV.model_validate(cv_dict)


def test_render_dict_fills_the_template(cv_dict, tmp_path):
    cv_dict["projects"][0].pop("linkText")
    cv_dict["projects"][0].pop("linkUrl")
    rendered = CV.model_validate(cv_dict).to_render_dict()
    assert "linkText" not in rendered["projects"][0]  # None must not reach the renderer
    out = tmp_path / "cv.docx"
    generate_docx(escape_ampersands(rendered), str(out))
    text = "\n".join(p.text for p in Document(str(out)).paragraphs)
    assert cv_dict["experience"][0]["bullets"][0] in text
    assert "{{" not in text and "{%" not in text


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["experience"][0]["bullets"].append("x" * 10), "at most"),
        (lambda d: d["experience"][0]["bullets"].__setitem__(0, "x" * (limits.MAX_BULLET_CHARS + 1)), "at most 220 characters"),
        (lambda d: d.__setitem__("headline", "x" * (limits.MAX_HEADLINE_CHARS + 1)), "at most 110 characters"),
        (lambda d: d.__setitem__("summary", "x" * (limits.MAX_SUMMARY_CHARS + 1)), "at most 550 characters"),
        (lambda d: d.__setitem__("summary", "Uses <script> tags"), "'<' or '>'"),
        (lambda d: d.__setitem__("title", "Senior Engineer"), "Extra inputs"),
        (lambda d: d.pop("projects"), "Field required"),
        (lambda d: d.pop("githubUrl"), "together"),
        (lambda d: d["experience"][0].__setitem__("bullets", []), "at least 1"),
        (lambda d: d["skills"][0].__setitem__("skills", ", ".join(f"s{i}" for i in range(limits.MAX_SKILLS_TOTAL + 1))), "skills"),
    ],
)
def test_rule_violations_are_rejected(cv_dict, mutate, message):
    data = copy.deepcopy(cv_dict)
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        CV.model_validate(data)


def test_split_list_respects_parentheses():
    assert split_list("NestJS (modules, DI), TypeScript; SQL: PostgreSQL") == [
        "NestJS (modules, DI)", "TypeScript", "SQL: PostgreSQL",
    ]
