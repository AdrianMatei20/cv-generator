import json
from pathlib import Path

import pytest

from cvgen import convert, profile
from cvgen.profile import check_profile, has_metric, load_profile
from cvgen.profile_schema import Job

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "profile.example.json"
SAMPLE = REPO / "tests" / "fixtures" / "data" / "sample.json"


def example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_example_profile_passes():
    result = check_profile(example())
    assert result.errors == []
    assert len(result.profile.index()) == sum(1 for _ in result.profile.items())


def test_compiled_profile_fields_are_accepted():
    data = example()
    data["preferences"] = {"target_roles": ["Backend Engineer"], "never_claim": ["Kotlin"], "tone": "plain"}
    data["awards"] = [{"id": "award-hackathon", "name": "Hackathon finalist", "date": "2025-09", "sources": ["a.json"]}]
    data["summaries"][0]["target_role"] = "Backend Engineer"
    bullet = data["experience"][0]["bullets"][0]
    bullet.update({"facts": ["3 card networks"], "angles": ["technical"], "sources": ["a.json", "old/b.json"]})
    data["experience"][1]["positions"] = [{"company": "Globex", "title": "Intern", "start": "2020-01", "end": "2020-03"}]
    data["projects"][0]["compact_bullets"] = [{"id": "proj-statuspage-compact-1", "text": "Uptime monitor for 50 endpoints"}]
    result = check_profile(data)
    assert result.errors == []
    assert {"award-hackathon", "proj-statuspage-compact-1"} <= set(result.profile.index())

    data["projects"][0]["compact_bullets"][0]["id"] = "proj-statuspage-01"
    assert any("duplicate id 'proj-statuspage-01'" in e for e in check_profile(data).errors)


def test_check_command_exit_codes(tmp_path, capsys):
    assert profile.main(["check", str(EXAMPLE)]) == 0
    bad = example()
    del bad["experience"][0]["bullets"][1]["id"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    assert profile.main(["check", str(path)]) == 1
    assert "experience[0].bullets[1]: missing id" in capsys.readouterr().out


def test_missing_and_duplicate_ids_are_errors():
    data = example()
    data["experience"][0]["bullets"][0]["id"] = None
    data["projects"][0]["bullets"][0]["id"] = "exp-acme-02"
    errors = check_profile(data).errors
    assert "experience[0].bullets[0]: missing id" in errors
    assert any("duplicate id 'exp-acme-02'" in e and "projects[0].bullets[0]" in e for e in errors)


def test_malformed_id_and_date_are_errors():
    data = example()
    data["skills"][0]["skills"][0]["id"] = "Skill Node"
    data["experience"][0]["start"] = "March 2023"
    errors = check_profile(data).errors
    assert any(e.startswith("skills.0.skills.0.id") for e in errors)
    assert any(e.startswith("experience.0.start") for e in errors)


def test_load_profile_rejects_errors(tmp_path):
    data = example()
    data["summaries"][1]["id"] = data["summaries"][0]["id"]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(profile.ProfileError, match="duplicate id"):
        load_profile(path)
    assert load_profile(EXAMPLE).contact.name == "Jane Doe"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Cut p95 latency by 40%", True),
        ("Served 1,200 merchants", True),
        ("Mentored two engineers", True),
        ("Scaled throughput 3x", True),
        ("Removed connections to hundreds of servers", True),
        ("Built REST endpoints with class-validator DTOs", False),
        ("Deployed on k3s with AWS SDK v3", False),
    ],
)
def test_has_metric(text, expected):
    assert has_metric(text) is expected


def test_job_period_format():
    job = Job(company="A", title="B", location="Remote", start="2025-09", end=None)
    assert job.period == "Sep 2025 – Present"


@pytest.mark.parametrize(
    "period, expected",
    [
        ("Nov 2022 – Sept 2025", ("2022-11", "2025-09")),
        ("Jan 2026 – Present", ("2026-01", None)),
        ("June 2021 - Oct 2021", ("2021-06", "2021-10")),
    ],
)
def test_parse_period(period, expected):
    assert convert.parse_period(period) == expected


def test_converter_output_passes_check(tmp_path):
    out = tmp_path / "profile.json"
    assert convert.main([str(SAMPLE), "-o", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert check_profile(data).errors == []
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    bullets = [b["text"] for job in data["experience"] for b in job["bullets"]]
    assert bullets == [b for job in sample["experience"] for b in job["bullets"]]  # nothing added or lost
    assert data["experience"][0]["id"] == "exp-brainspotting"
    assert data["experience"][0]["bullets"][2]["id"] == "exp-brainspotting-03"


def test_converter_merges_and_dedupes(tmp_path):
    newer = json.loads(SAMPLE.read_text(encoding="utf-8"))
    older = json.loads(SAMPLE.read_text(encoding="utf-8"))
    older["experience"][0]["period"] = "Jan 2026 – Present"
    older["experience"][0]["title"] += " (Contract)"
    older["experience"][0]["bullets"] = [newer["experience"][0]["bullets"][0] + ".", "An older wording"]
    merged = convert.build_profile([newer, older])
    job = merged["experience"][0]
    assert len(merged["experience"]) == len(newer["experience"])
    assert job["end"] == "2026-04"  # newest wins
    assert [b["text"] for b in job["bullets"]][-1] == "An older wording"
    assert len(job["bullets"]) == len(newer["experience"][0]["bullets"]) + 1


def test_converter_never_overwrites(tmp_path):
    out = tmp_path / "profile.json"
    out.write_text("{}", encoding="utf-8")
    assert convert.main([str(SAMPLE), "-o", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == "{}"
