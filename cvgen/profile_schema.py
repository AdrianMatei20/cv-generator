"""Pydantic models for profile.json, the master profile and only source of facts.

A superset of the CV: every job, project, skill and bullet the candidate might
use, each with a stable id so a tailored CV can be traced back to it. Ids are
optional in the models so `python -m cvgen.profile check` can report every
missing one at once; cvgen.profile.load_profile() rejects a profile with
missing or duplicate ids.
"""

from typing import Annotated, Iterator, Optional

from pydantic import BaseModel, ConfigDict, StringConstraints

ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
YearMonth = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]  # "2026-01"


def format_month(value: Optional[str]) -> str:
    """'2026-01' -> 'Jan 2026'; None -> 'Present'."""
    if value is None:
        return "Present"
    year, month = value.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Item(_Model):
    id: Optional[Id] = None
    tags: list[Text] = []  # free-form, e.g. "backend", "leadership", "python"
    sources: list[Text] = []  # CV JSON files (relative to data/) this item was compiled from


class Link(_Model):
    text: Text  # shown on the CV, e.g. "github.com/janedoe"
    url: Text


class Contact(_Model):
    name: Text
    phone: Text
    email: Text
    location: Optional[Text] = None
    linkedin: Optional[Link] = None
    github: Optional[Link] = None


class TextVariant(Item):
    text: Text
    target_role: Optional[Text] = None  # e.g. "Backend Engineer (Node.js)"


class Bullet(Item):
    text: Text
    facts: list[Text] = []  # extra numbers/tech/scope found only in other wordings of this bullet
    angles: list[Text] = []  # what the bullet demonstrates, e.g. "leadership", "technical"
    position: Optional[Text] = None  # which of a job's positions it belongs to, if the job has several


class Position(_Model):
    """One of several positions shown as a single CV entry (e.g. internships)."""
    company: Text
    title: Text
    start: YearMonth
    end: Optional[YearMonth] = None
    sources: list[Text] = []


class Job(Item):
    company: Text
    title: Text
    location: Text  # as shown on the CV, e.g. "4-month contract, Remote"
    start: YearMonth
    end: Optional[YearMonth] = None  # None means "Present"
    positions: list[Position] = []
    bullets: list[Bullet] = []

    @property
    def period(self) -> str:
        return f"{format_month(self.start)} – {format_month(self.end)}"


class Project(Item):
    name: Text
    description: Text
    link: Optional[Link] = None
    bullets: list[Bullet] = []
    # Optional shorter alternative to `bullets` for when space is tight. Each one
    # may summarise several bullets, so a CV uses one set or the other, never both.
    compact_bullets: list[Bullet] = []


class Education(Item):
    institution: Text
    degree: Text
    graduated: YearMonth


class Certification(Item):
    name: Text
    date: Optional[YearMonth] = None
    status: Optional[Text] = None  # e.g. "In preparation", shown when there is no date
    details: Optional[Text] = None


class Skill(Item):
    name: Text
    facts: list[Text] = []  # qualifiers seen in sources, e.g. "k3s", "consumer groups"


class SkillGroup(_Model):
    category: Text
    skills: list[Skill]


class Language(Item):
    language: Text
    level: Text  # e.g. "Native", "C1"


class Award(Item):
    name: Text
    date: Optional[YearMonth] = None
    details: Optional[Text] = None


class Availability(Item):
    text: Text  # shown verbatim on its own line under the headline, e.g. "Willing to relocate to Bucharest"


class Preferences(_Model):
    """Filled in by hand; guides tailoring. Never a source of facts, except `availability`: what the
    candidate states about where and how they'll work, picked by id and copied verbatim into the CV."""
    target_roles: list[Text] = []
    never_claim: list[Text] = []  # things the CV must never say, even if a source file did
    tone: str = ""
    availability: list[Availability] = []


class Profile(_Model):
    contact: Contact
    headlines: list[TextVariant] = []
    summaries: list[TextVariant] = []
    experience: list[Job] = []
    projects: list[Project] = []
    education: list[Education] = []
    certifications: list[Certification] = []
    skills: list[SkillGroup] = []
    languages: list[Language] = []
    awards: list[Award] = []
    preferences: Preferences = Preferences()

    def items(self) -> Iterator[tuple[str, Item]]:
        """Yield (path, item) for every item that carries an id, in file order."""
        for key in ("headlines", "summaries"):
            for i, item in enumerate(getattr(self, key)):
                yield f"{key}[{i}]", item
        for key in ("experience", "projects"):
            for i, item in enumerate(getattr(self, key)):
                yield f"{key}[{i}]", item
                for j, bullet in enumerate(item.bullets):
                    yield f"{key}[{i}].bullets[{j}]", bullet
                for j, bullet in enumerate(getattr(item, "compact_bullets", [])):
                    yield f"{key}[{i}].compact_bullets[{j}]", bullet
        for key in ("education", "certifications", "languages", "awards"):
            for i, item in enumerate(getattr(self, key)):
                yield f"{key}[{i}]", item
        for i, group in enumerate(self.skills):
            for j, skill in enumerate(group.skills):
                yield f"skills[{i}].skills[{j}]", skill
        for i, item in enumerate(self.preferences.availability):
            yield f"preferences.availability[{i}]", item

    def index(self) -> dict[str, Item]:
        """Map id -> item. Assumes ids are present and unique (see load_profile)."""
        return {item.id: item for _, item in self.items() if item.id}
