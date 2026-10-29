"""Per-call cost rows, prices, costs.csv, the 7/30-day report, and the bench table. Offline."""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from cvgen import costs, layout, pipeline, settings

EXAMPLE = Path(__file__).resolve().parent.parent / "profile.example.json"


def message(model="claude-opus-5-5", inp=702, out=9569, write=22583, read=0, write_1h=0, searches=0):
    usage = SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=write, cache_read_input_tokens=read,
                            cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=write - write_1h, ephemeral_1h_input_tokens=write_1h),
                            server_tool_use=SimpleNamespace(web_search_requests=searches))
    return SimpleNamespace(model=model, usage=usage)


def test_call_row_and_price():
    row = costs.call_row("tailor", message(), "high")
    assert {k: row[k] for k in ("step", "model", "effort", "input_tokens", "cache_write_5m_tokens", "cache_read_tokens",
                                "output_tokens", "web_searches")} == {
        "step": "tailor", "model": "claude-opus-5-5", "effort": "high", "input_tokens": 702,
        "cache_write_5m_tokens": 22583, "cache_read_tokens": 0, "output_tokens": 9569, "web_searches": 0}
    # 702 x $4 + 22,583 x $5 (5-minute write) + 9,569 x $20, per million
    assert row["cost_usd"] == pytest.approx((702 * 4 + 22583 * 5 + 9569 * 20) / 1e6)


def test_cache_reads_1h_writes_and_searches_are_priced():
    row = costs.call_row("gap report", message(inp=3970, out=12643, write=1000, write_1h=1000, read=108019, searches=5), "high")
    assert row["cache_write_5m_tokens"] == 0 and row["cache_write_1h_tokens"] == 1000
    expected = (3970 * 4 + 1000 * 8 + 108019 * 0.20 + 12643 * 20) / 1e6 + 5 * 0.01
    assert row["cost_usd"] == pytest.approx(expected)


def test_other_models_and_long_context_haiku():
    sonnet = costs.call_row("tailor", message("claude-sonnet-5-5", inp=1_000_000, out=0, write=0, read=1_000_000), "high")
    assert sonnet["cost_usd"] == pytest.approx(2.00 + 0.10)  # Sonnet 5.5 cache reads are $0.10 on the pricing page
    short = costs.call_row("tailor", message("claude-haiku-5-5", inp=50_000, out=0, write=0), "low")
    long = costs.call_row("tailor", message("claude-haiku-5-5", inp=50_000, out=0, write=0, read=60_000), "low")
    assert short["cost_usd"] == pytest.approx(50_000 * 0.10 / 1e6)
    assert long["cost_usd"] == pytest.approx((50_000 * 0.50 + 60_000 * 0.05) / 1e6)  # over 100K: higher prices
    assert costs.call_row("x", message("some-future-model"), None)["cost_usd"] is None
    assert costs.totals([sonnet, costs.call_row("x", message("some-future-model"), None)])["cost_usd"] is None


def test_buckets_and_totals():
    rows = [costs.call_row(step, message(inp=1_000_000, out=0, write=0), "high")
            for step in ("tailor", "repair", "layout round 1", "layout round 2", "gap report", "gap repair")]
    assert costs.by_bucket(rows) == {"tailor": 4.0, "repair": 4.0, "layout": 8.0, "gap_report": 8.0}
    t = costs.totals(rows)
    assert t["calls"] == 6 and t["input_tokens"] == 6_000_000 and t["cost_usd"] == pytest.approx(24.0)


def test_csv_and_report(tmp_path):
    path = tmp_path / "costs.csv"
    cheap = [costs.call_row("tailor", message(inp=100_000, out=0, write=0), "high")]  # $0.40
    pricey = cheap + [costs.call_row("gap report", message(inp=0, out=50_000, write=0, searches=10), "high")]  # + $1.10
    costs.append_csv(path, "Old_Job_2026-09-01", "claude-opus-5-5", cheap, day=date(2026, 9, 1))
    costs.append_csv(path, "A_Job_2026-10-25", "claude-opus-5-5", cheap, day=date(2026, 10, 25))
    costs.append_csv(path, "B_Job_2026-10-29", "claude-opus-5-5", pricey, day=date(2026, 10, 29))
    lines = costs.read_csv(path)
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(costs.CSV_FIELDS)
    assert lines[2]["folder"] == "B_Job_2026-10-29" and float(lines[2]["gap_report_usd"]) == pytest.approx(1.10)
    week = costs.report(lines, 7, today=date(2026, 10, 29))
    month = costs.report(lines, 30, today=date(2026, 10, 29))
    assert week["cvs"] == 2 and week["per_cv"] == pytest.approx((0.40 + 1.50) / 2)
    assert week["per_step"]["gap_report"] == pytest.approx(0.55) and week["per_step"]["tailor"] == pytest.approx(0.40)
    assert month["cvs"] == 2  # Sep 1 is outside the last 30 days
    assert costs.report(lines, 7, today=date(2027, 1, 1))["per_cv"] is None


def test_bench_table_and_drivers():
    rows = [costs.call_row("tailor", message(inp=700, out=10_000, write=23_000), "high"),
            costs.call_row("gap report", message(inp=4_000, out=13_000, write=0, read=100_000, searches=5), "high"),
            costs.call_row("layout round 1", message(inp=3_000, out=1_500, write=0), "medium")]
    table = costs.bench_table(rows)
    assert [t["step"] for t in table] == ["gap report", "tailor", "layout round 1"]  # by cost, most expensive first
    drivers = costs.cost_drivers(rows)
    assert drivers[0][:2] == ("gap report", "output tokens (incl. thinking)")
    assert drivers[1][:2] == ("tailor", "output tokens (incl. thinking)")
    assert drivers[2][:2] == ("tailor", "cache writes")


# ---------------------------------------------------------------- pipeline records calls and costs.csv
class FakeClient:
    def __init__(self, messages):
        self.out = list(messages)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        m = self.out.pop(0)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: m)

            def __exit__(self_inner, *exc):
                return False
        return Ctx()


def tool_message(name, data, **usage):
    m = message(**usage)
    m.content = [SimpleNamespace(type="tool_use", name=name, id="toolu_1", input=data, model_dump=lambda: {"type": "tool_use"})]
    m.stop_reason, m.stop_details = "tool_use", None
    return m


SELECTION = {
    "company": "Acme", "role": "Engineer",
    "headline": {"text": "Backend Engineer", "source_ids": ["headline-backend"]},
    "summary": {"text": "Backend engineer with 6 years building Node.js services.", "source_ids": ["summary-backend"]},
    "skills": [{"category": "Core", "skills": "Node.js, TypeScript", "source_ids": ["skill-nodejs", "skill-typescript"]}],
    "experience": [{"job_id": "exp-acme", "bullets": [{"text": "Cut p95 latency from 900ms to 220ms", "source_ids": ["exp-acme-02"]}]},
                   {"job_id": "exp-globex", "bullets": [{"text": "Designed a PostgreSQL schema for 300 clients", "source_ids": ["exp-globex-01"]}]}],
    "projects": [], "education_ids": ["edu-bsc"], "certification_ids": ["cert-cka"], "gaps": [],
}


def test_pipeline_records_every_call_and_appends_csv(tmp_path, monkeypatch):
    def fake_render(cv, out_dir, basename, template=None):
        doc = pymupdf.open()
        page = doc.new_page()
        for i, text in enumerate([cv["summary"], *(b for e in cv["experience"] for b in e["bullets"])]):
            page.insert_text((40, 60 + 14 * i), text, fontname="helv", fontsize=9)
        doc.save(str(Path(out_dir) / f"{basename}.pdf"))
        return Path(out_dir) / f"{basename}.docx", Path(out_dir) / f"{basename}.pdf"
    monkeypatch.setattr(layout, "render", fake_render)
    monkeypatch.setattr(pipeline, "render", fake_render)
    bad = json.loads(json.dumps(SELECTION))
    bad["experience"][1]["bullets"][0]["text"] = "Designed a PostgreSQL schema for 450 clients"  # forces one repair
    client = FakeClient([tool_message("submit_cv", bad, inp=700, out=9000, write=23000),
                         tool_message("submit_cv", SELECTION, inp=9000, out=4000, write=0, read=23000)])
    folder = pipeline.run("JD", profile_path=EXAMPLE, output_root=tmp_path, client=client, gaps=False)
    meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
    assert [c["step"] for c in meta["calls"]] == ["tailor", "repair"]
    assert all(c["effort"] == settings.tailor_effort() and c["model"] == "claude-opus-5-5" for c in meta["calls"])
    assert meta["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in meta["calls"]))
    assert meta["cost_by_step"]["repair"] == pytest.approx(meta["calls"][1]["cost_usd"])
    lines = costs.read_csv(tmp_path / "costs.csv")
    assert len(lines) == 1 and lines[0]["folder"] == folder.name and lines[0]["calls"] == "2"
    assert float(lines[0]["total_usd"]) == pytest.approx(meta["cost_usd"])
