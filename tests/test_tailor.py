"""Offline tests for cvgen.tailor: no API calls (a fake client stands in)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cvgen import tailor as tailor_mod
from cvgen.profile import check_profile
from cvgen.schema import CV
from cvgen.tailor import TOOL, TailorError, TailoredCV, assemble, build_request, tailor

EXAMPLE = Path(__file__).resolve().parent.parent / "profile.example.json"


@pytest.fixture
def profile():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def selection(**overrides):
    data = {
        "company": "Acme", "role": "Backend Engineer",
        "headline": {"text": "Backend Engineer – Node.js / TypeScript", "source_ids": ["headline-backend"]},
        "summary": {"text": "Backend engineer with 6 years building Node.js services.", "source_ids": ["summary-backend"]},
        "skills": [{"category": "Core", "skills": "Node.js, TypeScript", "source_ids": ["skill-nodejs", "skill-typescript"]}],
        "experience": [
            {"job_id": "exp-globex", "bullets": [{"text": "Designed a PostgreSQL schema for 300 clients", "source_ids": ["exp-globex-01"]}]},
            {"job_id": "exp-acme", "bullets": [{"text": "Cut p95 latency from 900ms to 220ms", "source_ids": ["exp-acme-02"]}]},
        ],
        "projects": [{"project_id": "proj-statuspage", "bullets": [{"text": "Polls 50 endpoints every 30s", "source_ids": ["proj-statuspage-01"]}]}],
        "education_ids": ["edu-bsc"],
        "certification_ids": ["cert-aws-saa", "cert-cka"],
        "gaps": ["Go"],
    }
    data.update(overrides)
    return TailoredCV.model_validate(data)


def test_tool_schema_is_strict():
    def walk(node):
        if isinstance(node, dict):
            for banned in ("maxLength", "minLength", "maxItems", "minItems", "default"):
                assert banned not in node
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    assert TOOL["strict"] is True
    walk(TOOL["input_schema"])


def test_request_caches_everything_but_the_jd(profile):
    a = build_request(profile, "JD one", "claude-opus-5-5", "high")
    b = build_request(profile, "JD two", "claude-opus-5-5", "high")
    assert a["system"] == b["system"] and a["tools"] == b["tools"]
    assert a["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "JD one" in a["messages"][0]["content"] and "JD one" not in json.dumps(a["system"])
    assert a["tool_choice"]["type"] == "auto"  # Opus 5.5 rejects forced tool_choice
    assert '"sources"' not in a["system"][1]["text"]


def test_assemble_fills_facts_from_profile(profile):
    checked = check_profile(profile)
    cv, sources, problems = assemble(selection(), checked.profile)
    assert problems == []
    CV.model_validate(cv)
    assert [j["company"] for j in cv["experience"]] == ["Acme Payments", "Globex Logistics"]  # newest first
    assert cv["experience"][0]["period"] == "Mar 2023 – Present"
    assert cv["certificates"] == [
        {"certificate": "AWS Certified Solutions Architect – Associate", "date": "In preparation"},
        {"certificate": "Certified Kubernetes Administrator (CKA)", "date": "May 2024"},
    ]
    assert cv["linkedinUrl"] == "https://www.linkedin.com/in/janedoe"
    assert sources["experience"][0] == {"id": "exp-acme", "bullets": [["exp-acme-02"]]}
    assert "source_ids" not in json.dumps(cv)


def test_assemble_reports_bad_ids(profile):
    checked = check_profile(profile)
    bad = selection(experience=[
        {"job_id": "exp-acme", "bullets": [{"text": "Borrowed bullet", "source_ids": ["exp-globex-01"]}]},
        {"job_id": "exp-nope", "bullets": [{"text": "x", "source_ids": ["exp-acme-01"]}]},
    ], education_ids=["exp-acme"])
    _, _, problems = assemble(bad, checked.profile)
    assert any("'exp-globex-01' doesn't belong here" in p for p in problems)
    assert any("'exp-nope' is not a profile job" in p for p in problems)
    assert any("'exp-acme' is not a profile education" in p for p in problems)


def test_assemble_reports_mixed_compact_bullets(profile):
    profile["projects"][0]["compact_bullets"] = [{"id": "proj-statuspage-c1", "text": "Uptime monitor for 50 endpoints"}]
    checked = check_profile(profile)
    mixed = selection(projects=[{"project_id": "proj-statuspage", "bullets": [
        {"text": "a", "source_ids": ["proj-statuspage-01"]}, {"text": "b", "source_ids": ["proj-statuspage-c1"]}]}])
    _, _, problems = assemble(mixed, checked.profile)
    assert any("mixes bullets and compact_bullets" in p for p in problems)


def test_assemble_reports_limit_and_never_claim_violations(profile):
    profile["preferences"] = {"never_claim": ["Kotlin"]}
    checked = check_profile(profile)
    long = selection(summary={"text": "Kotlin " + "x" * 600, "source_ids": ["summary-backend"]})
    _, _, problems = assemble(long, checked.profile)
    assert any(p.startswith("summary:") and "at most" in p for p in problems)
    assert any("never_claim" in p for p in problems)


class FakeClient:
    """Mimics client.beta.messages.stream(...).get_final_message() for canned messages."""

    def __init__(self, messages):
        self.messages_out = list(messages)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        message = self.messages_out.pop(0)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: message)

            def __exit__(self_inner, *exc):
                return False
        return Ctx()


def fake_message(content, stop_reason="tool_use", cache_read=0):
    usage = SimpleNamespace(input_tokens=50, output_tokens=900, cache_creation_input_tokens=0, cache_read_input_tokens=cache_read)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage, model="claude-opus-5-5", stop_details=None)


def test_tailor_returns_result_and_logs_usage(profile, caplog):
    call = SimpleNamespace(type="tool_use", name="submit_cv", input=selection().model_dump())
    client = FakeClient([fake_message([call], cache_read=12000)])
    with caplog.at_level("INFO", logger="cvgen.tailor"):
        result = tailor(profile, "Some JD", client=client)
    assert result.company == "Acme" and result.problems == []
    assert result.meta()["usage"]["cache_read_input_tokens"] == 12000
    assert "cache_read=12000" in caplog.text
    assert client.requests[0]["model"] == tailor_mod.DEFAULT_MODEL


def test_tailor_retries_once_without_tool_call(profile):
    text = SimpleNamespace(type="text", text="Here is your CV...")
    client = FakeClient([fake_message([text], "end_turn"), fake_message([text], "end_turn")])
    with pytest.raises(TailorError, match="didn't call submit_cv"):
        tailor(profile, "Some JD", client=client)
    assert len(client.requests) == 2


def test_tailor_respects_cv_model_env(profile, monkeypatch):
    monkeypatch.setenv("CV_MODEL", "claude-sonnet-5-5")
    call = SimpleNamespace(type="tool_use", name="submit_cv", input=selection().model_dump())
    client = FakeClient([fake_message([call])])
    tailor(profile, "JD", client=client)
    assert client.requests[0]["model"] == "claude-sonnet-5-5"
