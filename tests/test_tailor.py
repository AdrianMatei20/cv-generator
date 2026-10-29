"""Offline tests for cvgen.tailor: no API calls (a fake client stands in)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cvgen import tailor as tailor_mod
from cvgen.profile import check_profile
from cvgen.schema import CV
from cvgen.tailor import (MAX_REPAIRS, TOOL, TailorError, TailoredCV, TailorValidationError, assemble, build_request,
                          tailor)

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


def invented():
    """A selection with an invented number (450), which validate() rejects."""
    return selection(experience=[
        {"job_id": "exp-globex", "bullets": [{"text": "Designed a PostgreSQL schema for 450 clients", "source_ids": ["exp-globex-01"]}]},
        {"job_id": "exp-acme", "bullets": [{"text": "Cut p95 latency from 900ms to 220ms", "source_ids": ["exp-acme-02"]}]},
    ])


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
    assert a["system"][1]["cache_control"] == {"type": "ephemeral"}  # breakpoint on the profile
    assert "cache_control" not in a["system"][2]  # the task prompt comes after it
    assert "JD one" in a["messages"][0]["content"] and "JD one" not in json.dumps(a["system"])
    assert a["tool_choice"]["type"] == "auto"  # Opus 5.5 rejects forced tool_choice
    assert '"sources"' not in a["system"][1]["text"]


def test_gap_call_shares_the_cached_prefix(profile, monkeypatch):
    """Tools, betas, shared rules and profile must be byte-identical across tailor and gap calls,
    or the gap call re-writes the whole profile to the cache."""
    from cvgen.gaps import build_gap_request
    for flag in ("1", "0"):
        monkeypatch.setenv("GAP_WEB_SEARCH", flag)
        t = build_request(profile, "JD", "claude-opus-5-5", "high")
        g = build_gap_request(profile, "JD", "claude-opus-5-5", "high", web_search=flag == "1")
        assert json.dumps(t["tools"]) == json.dumps(g["tools"]) and t["betas"] == g["betas"]
        assert json.dumps(t["system"][:2]) == json.dumps(g["system"][:2])
        assert t["system"][2] != g["system"][2]
    assert [tool.get("defer_loading", False) for tool in t["tools"]] == [False, True]  # search off: not declared
    changes = g["messages"][1]["content"]
    assert g["messages"][1]["role"] == "system"
    assert [(c["type"], c["tool"]["name"]) for c in changes] == [("tool_removal", "submit_cv"), ("tool_addition", "submit_gap_report")]


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


def test_assemble_reports_ids_it_cannot_place(profile):
    checked = check_profile(profile)
    bad = selection(experience=[
        {"job_id": "exp-acme", "bullets": [{"text": "Cut p95 latency from 900ms to 220ms", "source_ids": ["exp-acme-02"]}]},
        {"job_id": "exp-nope", "bullets": [{"text": "x", "source_ids": ["exp-acme-01"]}]},
    ], education_ids=["exp-acme"])
    cv, sources, problems = assemble(bad, checked.profile)
    assert problems == ["experience job_id #2: 'exp-nope' is not a profile job, so it was left out",
                        "education_ids #1: 'exp-acme' is not a profile education, so it was left out"]
    assert [j["company"] for j in cv["experience"]] == ["Acme Payments"] and sources["education"] == []


class FakeClient:
    """Mimics client.beta.messages.stream(...).get_final_message() for canned messages."""

    def __init__(self, messages):
        self.messages_out = list(messages)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
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


def tool_call(sel, call_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name="submit_cv", id=call_id, input=sel.model_dump())


def test_tailor_returns_result_and_logs_usage(profile, caplog):
    client = FakeClient([fake_message([tool_call(selection())], cache_read=12000)])
    with caplog.at_level("INFO", logger="cvgen.tailor"):
        result = tailor(profile, "Some JD", client=client)
    assert result.company == "Acme" and result.problems == [] and result.repairs == []
    assert result.meta()["usage"]["cache_read_input_tokens"] == 12000
    assert "cache_read=12000" in caplog.text
    assert client.requests[0]["model"] == tailor_mod.DEFAULT_MODEL


def test_tailor_nudges_once_without_tool_call(profile):
    text = SimpleNamespace(type="text", text="Here is your CV...")
    client = FakeClient([fake_message([text], "end_turn"), fake_message([text], "end_turn")])
    with pytest.raises(TailorError, match="didn't call submit_cv"):
        tailor(profile, "Some JD", client=client)
    assert len(client.requests) == 2
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": "Submit the CV by calling submit_cv."}


def test_tailor_respects_cv_model_env(profile, monkeypatch):
    monkeypatch.setenv("CV_MODEL", "claude-sonnet-5-5")
    client = FakeClient([fake_message([tool_call(selection())])])
    tailor(profile, "JD", client=client)
    assert client.requests[0]["model"] == "claude-sonnet-5-5"


def test_tailor_repairs_in_the_same_conversation(profile):
    client = FakeClient([fake_message([tool_call(invented(), "toolu_a")]), fake_message([tool_call(selection(), "toolu_b")])])
    result = tailor(profile, "JD", client=client)
    assert result.problems == []
    assert result.repairs == [{"attempt": 1, "errors": [
        "experience[1] (Globex Logistics).bullets[0]: number(s) 450 not found in its source items; use only numbers the profile gives"]}]
    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"][0].input["experience"][0]["bullets"][0]["text"].endswith("450 clients")  # previous output sent back
    tool_result = second[2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_a" and tool_result["is_error"]
    assert "450 not found" in tool_result["content"]
    assert client.requests[0]["system"] == client.requests[1]["system"]  # cached prefix unchanged
    assert result.usage["output_tokens"] == 1800 and len(result.usage["calls"]) == 2


def test_tailor_gives_up_after_max_repairs_and_saves_draft(profile, tmp_path):
    client = FakeClient([fake_message([tool_call(invented(), f"toolu_{i}")]) for i in range(MAX_REPAIRS + 1)])
    with pytest.raises(TailorValidationError) as info:
        tailor(profile, "JD", client=client, draft_dir=tmp_path)
    assert len(client.requests) == MAX_REPAIRS + 1
    message = str(info.value)
    assert f"after {MAX_REPAIRS} repair attempts" in message and "450 not found" in message
    assert str(tmp_path / "cv.draft.json") in message
    draft = json.loads((tmp_path / "cv.draft.json").read_text(encoding="utf-8"))
    meta = json.loads((tmp_path / "cv.draft.meta.json").read_text(encoding="utf-8"))
    assert draft["experience"][1]["bullets"][0].endswith("450 clients")
    assert len(meta["repairs"]) == MAX_REPAIRS + 1 and meta["problems"]
