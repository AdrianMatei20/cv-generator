"""Pydantic models for the CV JSON that cvgen.render.render() consumes.

Shapes match templates/cv_template_with_summary.docx exactly (see CLAUDE.md).
Length limits come from cvgen.limits. Use CV.to_render_dict() to get the dict
for render(): it drops unset optional keys, because the renderer treats a
present-but-None link field as a link to build.
"""

import re
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cvgen import limits


def _no_markup(value: str) -> str:
    # render() escapes '&' only; '<' or '>' would produce a broken docx.
    if "<" in value or ">" in value:
        raise ValueError("must not contain '<' or '>'")
    return value


def _text(max_length=None, min_length=1):
    # One StringConstraints per type: stacking a second one on an existing
    # Annotated type makes pydantic report string lengths as "items".
    return Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length),
        AfterValidator(_no_markup),
    ]


Text = _text()
OptionalText = _text(min_length=0)
Bullet = _text(limits.MAX_BULLET_CHARS)


def split_list(value: str) -> list[str]:
    """Split "a, b (c, d); e" on commas/semicolons outside parentheses."""
    items, depth, current = [], 0, []
    for ch in value:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(depth - 1, 0)
        if ch in ",;" and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    return [item.strip() for item in items if item.strip()]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_pair(obj, text_key, url_key):
    if (getattr(obj, text_key) is None) != (getattr(obj, url_key) is None):
        raise ValueError(f"{text_key} and {url_key} must be given together or not at all")


class SkillLine(_Model):
    category: Text
    skills: Text

    @model_validator(mode="after")
    def _line_length(self):
        length = len(self.category) + 2 + len(self.skills)
        if length > limits.MAX_SKILL_LINE_CHARS:
            raise ValueError(
                f"skills line '{self.category}' is {length} characters; max {limits.MAX_SKILL_LINE_CHARS}"
            )
        return self


class Job(_Model):
    title: Text
    company: Text
    location: Text
    period: Text
    bullets: list[Bullet] = Field(min_length=1, max_length=limits.MAX_BULLETS_PER_ROLE)


class Project(_Model):
    name: Text
    description: Text
    linkText: Optional[Text] = None
    linkUrl: Optional[Text] = None
    bullets: list[Bullet] = Field(min_length=1, max_length=limits.MAX_BULLETS_PER_PROJECT)

    @model_validator(mode="after")
    def _link_pair(self):
        _check_pair(self, "linkText", "linkUrl")
        return self


class Education(_Model):
    institution: Text
    degree: Text
    graduated: Text


class Certificate(_Model):
    certificate: Text
    date: OptionalText = ""
    details: Optional[Text] = None


class CV(_Model):
    name: Text
    headline: _text(limits.MAX_HEADLINE_CHARS)
    phone: Text
    email: Text
    linkedinText: Optional[Text] = None
    linkedinUrl: Optional[Text] = None
    githubText: Optional[Text] = None
    githubUrl: Optional[Text] = None
    summary: _text(limits.MAX_SUMMARY_CHARS)
    skills: list[SkillLine] = Field(min_length=1, max_length=limits.MAX_SKILL_CATEGORIES)
    experience: list[Job] = Field(min_length=1)
    projects: list[Project] = Field(max_length=limits.MAX_PROJECTS)
    education: list[Education]
    certificates: list[Certificate]

    @model_validator(mode="after")
    def _cv_rules(self):
        _check_pair(self, "linkedinText", "linkedinUrl")
        _check_pair(self, "githubText", "githubUrl")
        total = sum(len(split_list(line.skills)) for line in self.skills)
        if total > limits.MAX_SKILLS_TOTAL:
            raise ValueError(f"{total} skills listed; max {limits.MAX_SKILLS_TOTAL}")
        return self

    def to_render_dict(self) -> dict:
        return self.model_dump(exclude_none=True)
