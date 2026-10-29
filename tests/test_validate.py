"""validate() on hand-made good and bad CVs, built from profile.example.json."""

import copy
import json
from pathlib import Path

import pytest

from cvgen import limits
from cvgen.validate import validate

EXAMPLE = Path(__file__).resolve().parent.parent / "profile.example.json"


@pytest.fixture
def profile():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def good():
    """A valid CV + meta, as tailor() would assemble them from the example profile."""
    cv = {
        "name": "Jane Doe", "headline": "Backend Engineer – Node.js / TypeScript", "phone": "+40 700 000 000",
        "email": "jane.doe@example.com",
        "linkedinText": "linkedin.com/in/janedoe", "linkedinUrl": "https://www.linkedin.com/in/janedoe",
        "githubText": "github.com/janedoe", "githubUrl": "https://github.com/janedoe",
        "summary": "Backend engineer with 6 years building Node.js and TypeScript services for payments platforms.",
        "skills": [{"category": "Core", "skills": "Node.js, TypeScript"},
                   {"category": "Infrastructure", "skills": "Kubernetes, Prometheus"}],
        "experience": [
            {"title": "Senior Backend Engineer", "company": "Acme Payments", "location": "Hybrid", "period": "Mar 2023 – Present",
             "bullets": ["Cut p95 latency of the merchant API from 900ms to 220ms with Redis caching",
                         "Led root cause analysis for 12 severity-1 incidents and wrote the on-call runbooks"]},
            {"title": "Software Engineer", "company": "Globex Logistics", "location": "Remote", "period": "Jun 2020 – Feb 2023",
             "bullets": ["Designed a PostgreSQL schema and REST API used by 300 warehouse clients"]},
        ],
        "projects": [{"name": "StatusPage", "description": "self-hosted uptime monitor (Node.js, PostgreSQL, Kubernetes)",
                      "linkText": "github.com/janedoe/statuspage", "linkUrl": "https://github.com/janedoe/statuspage",
                      "bullets": ["Polls 50 endpoints every 30s and pages via Slack when 3 checks in a row fail"]}],
        "education": [{"institution": "Babes-Bolyai University", "degree": "BSc in Computer Science", "graduated": "Jun 2020"}],
        "certificates": [{"certificate": "Certified Kubernetes Administrator (CKA)", "date": "May 2024"},
                         {"certificate": "AWS Certified Solutions Architect – Associate", "date": "In preparation"}],
    }
    meta = {"sources": {
        "headline": ["headline-backend"], "summary": ["summary-backend"],
        "skills": [["skill-nodejs", "skill-typescript"], ["skill-kubernetes", "skill-prometheus"]],
        "experience": [{"id": "exp-acme", "bullets": [["exp-acme-02"], ["exp-acme-03"]]},
                       {"id": "exp-globex", "bullets": [["exp-globex-01"]]}],
        "projects": [{"id": "proj-statuspage", "bullets": [["proj-statuspage-01"]]}],
        "education": ["edu-bsc"], "certificates": ["cert-cka", "cert-aws-saa"],
    }}
    return cv, meta


def errors_after(good, profile, change):
    cv, meta = copy.deepcopy(good)
    change(cv, meta)
    return validate(cv, meta, profile)


def test_good_cv_has_no_errors(good, profile):
    assert validate(*good, profile) == []


def test_too_long_bullet(good, profile):
    long = "Cut p95 latency of the merchant API from 900ms to 220ms " + "x" * limits.MAX_BULLET_CHARS
    errors = errors_after(good, profile, lambda cv, m: cv["experience"][0]["bullets"].__setitem__(0, long))
    assert errors == [f"experience[0] (Acme Payments).bullets[0] is {len(long)} characters; max {limits.MAX_BULLET_CHARS}. Shorten it."]


def test_too_many_bullets_and_long_summary(good, profile):
    def change(cv, meta):
        cv["experience"][1]["bullets"] *= limits.MAX_BULLETS_PER_ROLE + 1
        meta["sources"]["experience"][1]["bullets"] *= limits.MAX_BULLETS_PER_ROLE + 1
        cv["summary"] = "Backend engineer. " * 40
    errors = errors_after(good, profile, change)
    assert any("experience[1] (Globex Logistics) has 7 bullets; max 6" in e for e in errors)
    assert any(e.startswith("summary is 720 characters") for e in errors)
    assert any("duplicates" in e for e in errors)


def test_invented_employer_and_changed_date(good, profile):
    def change(cv, meta):
        cv["experience"][1]["company"] = "Initech"
        cv["experience"][1]["period"] = "Jan 2019 – Feb 2023"
    errors = errors_after(good, profile, change)
    assert "experience[1] (Initech).company is 'Initech' but the profile says 'Globex Logistics'" in errors
    assert "experience[1] (Initech).period is 'Jan 2019 – Feb 2023' but the profile says 'Jun 2020 – Feb 2023'" in errors


def test_invented_number(good, profile):
    errors = errors_after(good, profile, lambda cv, m: cv["experience"][1]["bullets"].__setitem__(
        0, "Designed a PostgreSQL schema and REST API used by 450 warehouse clients, cutting costs 30%"))
    assert errors == ["experience[1] (Globex Logistics).bullets[0]: number(s) 30, 450 not found in its source items; "
                      "use only numbers the profile gives"]


def test_number_words_in_source_count(good, profile):
    # exp-acme-05 says "two junior engineers"; writing 2 is not an invented number
    def change(cv, meta):
        cv["experience"][0]["bullets"][1] = "Mentored 2 junior engineers through code review and pairing"
        meta["sources"]["experience"][0]["bullets"][1] = ["exp-acme-05"]
    assert errors_after(good, profile, change) == []


def test_unknown_and_foreign_source_ids(good, profile):
    def change(cv, meta):
        meta["sources"]["experience"][0]["bullets"][0] = ["exp-acme-99"]
        meta["sources"]["experience"][0]["bullets"][1] = ["exp-globex-02"]
        meta["sources"]["summary"] = ["summary-nope"]
    errors = errors_after(good, profile, change)
    assert "experience[0] (Acme Payments).bullets[0]: source id 'exp-acme-99' doesn't exist in profile.json" in errors
    assert "experience[0] (Acme Payments).bullets[1]: source id 'exp-globex-02' belongs to another entry, not exp-acme" in errors
    assert "summary: source id 'summary-nope' doesn't exist in profile.json" in errors


def test_unknown_skill_and_unrecorded_qualifier(good, profile):
    def change(cv, meta):
        cv["skills"][1]["skills"] = "Kubernetes (EKS), Terraform"
    errors = errors_after(good, profile, change)
    assert "skills[1] (Infrastructure): skill 'Terraform' isn't in the profile's skills" in errors
    assert "skills[1] (Infrastructure): 'Kubernetes (EKS)' adds 'EKS', which the profile doesn't record for Kubernetes" in errors


def test_schema_errors(good, profile):
    def change(cv, meta):
        del cv["projects"]
        meta["sources"]["projects"] = []
        cv["education"][0]["graduated"] = 2020
        cv["title"] = "extra"
    errors = errors_after(good, profile, change)
    assert "projects: Field required" in errors
    assert "education[0].graduated: Input should be a valid string" in errors
    assert "title: Extra inputs are not permitted" in errors


def test_hygiene(good, profile):
    def change(cv, meta):
        cv["experience"][0]["bullets"][0] = "I cut p95 latency of the merchant API from 900ms to 220ms "
        cv["experience"][0]["bullets"][1] = "Led root cause analysis for 12 severity-1  incidents for our team"
        cv["projects"][0]["description"] = " "
    errors = errors_after(good, profile, change)
    assert "experience[0].bullets[0] has leading or trailing spaces" in errors
    assert "experience[0].bullets[1] has a double space" in errors
    assert "projects[0].description is empty" in errors
    assert any("uses first person ('I')" in e for e in errors)
    assert any("uses first person ('our')" in e for e in errors)


def test_first_person_ignores_io_and_us_country(good, profile):
    def change(cv, meta):
        cv["experience"][0]["bullets"][1] = "Led root cause analysis for 12 severity-1 incidents in US I/O-heavy services"
    assert errors_after(good, profile, change) == []


def test_never_claim(good, profile):
    profile["preferences"] = {"never_claim": ["Kubernetes"]}
    errors = validate(*good, profile)
    assert "the CV mentions 'Kubernetes', which preferences.never_claim forbids" in errors


def test_mixed_compact_bullets(good, profile):
    profile["projects"][0]["compact_bullets"] = [{"id": "proj-statuspage-c1", "text": "Uptime monitor for 50 endpoints"}]

    def change(cv, meta):
        cv["projects"][0]["bullets"].append("Uptime monitor for 50 endpoints")
        meta["sources"]["projects"][0]["bullets"].append(["proj-statuspage-c1"])
    errors = errors_after(good, profile, change)
    assert errors == ["projects[0] (StatusPage): mixes its bullets and compact_bullets; use one set or the other"]
