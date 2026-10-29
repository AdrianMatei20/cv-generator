"""The local API server: security rules, job queue, stages, files. Offline (fake pipeline runner)."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cvgen import server
from cvgen.pipeline import PipelineError

PORT = 8765
TOKEN = "test-token-123"
SECRET = "sk-ant-api03-THIS-IS-NOT-A-REAL-KEY-0123456789"
BASE = f"http://127.0.0.1:{PORT}"
OK = {"Origin": "null", "X-CVGen-Token": TOKEN}


def make_folder(root: Path, name="Acme_Engineer_2026-10-29") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "CV_AdrianMatei.pdf").write_bytes(b"%PDF-1.7 test")
    (folder / "CV_AdrianMatei.docx").write_bytes(b"docx")
    (folder / "CV_JohnDoe.pdf").write_bytes(b"%PDF-1.7 anon")
    (folder / "CV_JohnDoe.docx").write_bytes(b"docx anon")
    (folder / "cv.json").write_text("{}", encoding="utf-8")
    meta = {"company": "Acme", "role": "Engineer", "model": "claude-opus-5-5", "cost_usd": 0.91, "gaps": ["Go"],
            "layout": {"final_pages": 2, "final_widows": 0, "kept_round": 1}, "gap_report": {"fit": 64}}
    (folder / "cv.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "gap_report.json").write_text(json.dumps({"fit": {"score": 64}, "requirements": [], "recommendations": [],
                                                        "quick_wins": []}), encoding="utf-8")
    return folder


class Runner:
    """Fake pipeline: reports the real stage messages, optionally waits, then returns a folder."""

    def __init__(self, root: Path, fail: Exception | None = None, gate: threading.Event | None = None):
        self.root, self.fail, self.gate, self.calls, self.n = root, fail, gate, [], 0

    def __call__(self, jd, company, role, pages, gaps, progress):
        self.calls.append((jd, company, role, pages, gaps))
        for message in ("Tailoring with claude-opus-5-5 (effort high)", "Validating", "Rendering (LibreOffice)",
                        "Layout check: 3 page(s), 1 widow(s)", "Layout round 1/2: fixing 1 widow(s)", "Gap report (with web search)"):
            progress(message)
        if self.gate:
            self.gate.wait(5)
        if self.fail:
            raise self.fail
        self.n += 1
        return make_folder(self.root, f"Acme_Engineer_2026-10-29{'_' + str(self.n) if self.n > 1 else ''}")


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    output = tmp_path / "output"
    output.mkdir()
    opened = []

    def build(runner=None):
        app = server.create_app(port=PORT, token=TOKEN, output_root=output, runner=runner or Runner(output),
                                opener=opened.append)
        client = TestClient(app, base_url=BASE)
        client.output, client.opened, client.app = output, opened, app
        return client
    return build


def wait_for(client, job_id, status="done", timeout=5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/jobs/{job_id}", headers=OK).json()
        if data["status"] == status:
            return data
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {status}: {data}")


# ---------------------------------------------------------------- security
def test_health_is_open_and_page_has_config_but_no_key(make_client):
    c = make_client()
    assert c.get("/api/health").json() == {"ok": True}
    page = c.get("/", headers={"Origin": "null"})
    assert page.status_code == 200 and f'{{"port": {PORT}, "token": "{TOKEN}"}}' in page.text
    assert SECRET not in page.text
    assert "access-control-allow-origin" not in page.headers  # other origins can't read the token


def test_post_needs_allowed_origin_and_token(make_client):
    c = make_client()
    body = {"jd": "Senior Node.js engineer"}
    assert c.post("/api/jobs", json=body, headers={"X-CVGen-Token": TOKEN}).status_code == 403  # no Origin
    assert c.post("/api/jobs", json=body, headers={"Origin": "https://evil.example", "X-CVGen-Token": TOKEN}).status_code == 403
    no_token = c.post("/api/jobs", json=body, headers={"Origin": "null"})
    assert no_token.status_code == 403 and "access token" in no_token.json()["error"]
    assert c.post("/api/jobs", json=body, headers={"Origin": "null", "X-CVGen-Token": "wrong"}).status_code == 403
    ok = c.post("/api/jobs", json=body, headers=OK)
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "null"
    ok2 = c.post("/api/jobs", json=body, headers={"Origin": BASE, "X-CVGen-Token": TOKEN})
    assert ok2.status_code == 200 and ok2.headers["access-control-allow-origin"] == BASE


def test_reads_need_the_token_too(make_client):
    c = make_client()
    assert c.get("/api/jobs", headers={"Origin": "null"}).status_code == 403
    assert c.get("/api/jobs", headers={"Origin": "https://evil.example", "X-CVGen-Token": TOKEN}).status_code == 403
    assert c.get("/api/jobs", headers=OK).status_code == 200


def test_wrong_host_is_rejected():
    app = server.create_app(port=PORT, token=TOKEN, runner=lambda *a: None)
    rebinding = TestClient(app, base_url="http://attacker.example:8765")
    assert rebinding.get("/api/health").status_code == 403


def test_preflight(make_client):
    c = make_client()
    r = c.options("/api/jobs", headers={"Origin": "null", "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "content-type,x-cvgen-token"})
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "null"
    assert "X-CVGen-Token" in r.headers["access-control-allow-headers"]
    assert c.options("/api/jobs", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.options("/files/x/y", headers={"Origin": "null"}).status_code == 403


def test_validation(make_client):
    c = make_client()
    assert c.post("/api/jobs", json={"jd": ""}, headers=OK).status_code == 422
    assert c.post("/api/jobs", json={"jd": "x", "pages": 3}, headers=OK).status_code == 422
    assert c.post("/api/jobs", json={"jd": "   "}, headers=OK).status_code == 422


# ---------------------------------------------------------------- jobs
def test_job_runs_and_reports_results(make_client):
    c = make_client()
    job_id = c.post("/api/jobs", json={"jd": "Senior Node.js engineer\nat Acme", "company": "Acme", "pages": 1,
                                        "gaps": True}, headers=OK).json()["id"]
    data = wait_for(c, job_id)
    assert data["stage"] == "done" and data["folder"] == "Acme_Engineer_2026-10-29"
    assert data["title"] == "Senior Node.js engineer"
    assert data["pdf"] == "/files/Acme_Engineer_2026-10-29/CV_AdrianMatei.pdf"
    assert data["docx"].endswith("CV_AdrianMatei.docx") and data["gap_report"]["fit"]["score"] == 64
    assert data["anon_pdf"] == "/files/Acme_Engineer_2026-10-29/CV_JohnDoe.pdf" and data["anon_docx"].endswith("CV_JohnDoe.docx")
    assert data["summary"]["fit"] == 64 and data["summary"]["cost_usd"] == 0.91
    assert data["layout"] == {"final_pages": 2, "final_widows": 0, "kept_round": 1}
    listing = c.get("/api/jobs", headers=OK).json()["jobs"]
    assert [j["id"] for j in listing] == [job_id]
    assert SECRET not in json.dumps(data) + json.dumps(listing)
    assert c.get(data["pdf"]).content == b"%PDF-1.7 test"


def test_jobs_run_one_at_a_time_and_report_stages(make_client):
    gate = threading.Event()
    c = make_client(Runner(None, gate=gate))
    c.app.state.queue.runner.root = c.output
    first = c.post("/api/jobs", json={"jd": "first"}, headers=OK).json()["id"]
    second = c.post("/api/jobs", json={"jd": "second"}, headers=OK).json()["id"]
    running = wait_for(c, first, status="running")
    deadline = time.monotonic() + 3
    while c.get(f"/api/jobs/{first}", headers=OK).json()["stage"] != "gap report" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert c.get(f"/api/jobs/{first}", headers=OK).json()["stage"] == "gap report"
    assert c.get(f"/api/jobs/{second}", headers=OK).json()["status"] == "queued"  # waits its turn
    assert running["status"] == "running"
    gate.set()
    wait_for(c, first)
    wait_for(c, second)


def test_failed_job_shows_a_clean_error(make_client):
    c = make_client(Runner(None, fail=PipelineError(f"API exploded, key {SECRET}")))
    job_id = c.post("/api/jobs", json={"jd": "x"}, headers=OK).json()["id"]
    data = wait_for(c, job_id, status="failed")
    assert data["stage"] == "failed" and "API exploded" in data["error"] and SECRET not in data["error"]
    assert "***" in data["error"]


def test_open_folder(make_client):
    c = make_client()
    job_id = c.post("/api/jobs", json={"jd": "x"}, headers=OK).json()["id"]
    wait_for(c, job_id)
    assert c.post(f"/api/jobs/{job_id}/open-folder", json={}, headers=OK).status_code == 200
    assert c.opened == [c.output.resolve() / "Acme_Engineer_2026-10-29"]
    assert c.post("/api/jobs/nope/open-folder", json={}, headers=OK).status_code == 404


def test_files_are_served_from_output_only(make_client, tmp_path):
    c = make_client()
    make_folder(c.output)
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (c.output / ".jobs.json").write_text("[]", encoding="utf-8")
    assert c.get("/files/Acme_Engineer_2026-10-29/CV_AdrianMatei.pdf").status_code == 200
    for path in ("/files/../secret.txt", "/files/..%2f..%2fsecret.txt/x", "/files/Acme_Engineer_2026-10-29/..%2f..%2fsecret.txt",
                 "/files/%2e%2e/secret.txt", "/files/Acme_Engineer_2026-10-29/.hidden", "/files/x/.jobs.json"):
        assert c.get(path).status_code == 404, path
    r = c.get("/files/Acme_Engineer_2026-10-29/CV_AdrianMatei.pdf", headers={"Origin": "null"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers  # no CORS on files


def test_jobs_survive_a_restart(tmp_path, monkeypatch):
    gate = threading.Event()
    output = tmp_path / "output"
    output.mkdir()
    queue = server.JobQueue(output, Runner(output, gate=gate))
    job = queue.submit("long job")
    deadline = time.monotonic() + 3
    while queue.get(job.id).status != "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    restarted = server.JobQueue(output, Runner(output))
    assert restarted.get(job.id).status == "failed" and "restarted" in restarted.get(job.id).error
    gate.set()


@pytest.mark.parametrize("message, stage", [
    ("Tailoring with claude-opus-5-5 (effort high)", "tailoring"),
    ("Validating", "validating"),
    ("Repairing 2 validation error(s) (attempt 1/2)", "validating"),
    ("Rendering (LibreOffice)", "rendering"),
    ("Layout check: 2 page(s), 0 widow(s)", "rendering"),
    ("Layout round 2/2: re-rendering", "layout round 2/2"),
    ("Gap report (with web search)", "gap report"),
    ("Repairing the gap report: 1 error(s) (attempt 1/2)", "gap report"),
    ("Re-rendering the kept version (round 1)", None),
])
def test_stage_for(message, stage):
    assert server.stage_for(message) == stage


def test_page_config_injection(tmp_path):
    html = server.page_html(9999, "abc")
    assert 'const CONFIG = /*CVGEN-CONFIG*/{"port": 9999, "token": "abc"}/*END-CVGEN-CONFIG*/;' in html
    repo_copy = server.UI_FILE.read_text(encoding="utf-8")
    assert '{"port": 8765, "token": ""}' in repo_copy  # the committed file carries no token
    assert "https://" not in repo_copy.split("<script>")[0] or "cdn" not in repo_copy.lower()  # no CDN


def test_gap_report_checkbox_reaches_the_pipeline(make_client):
    runner = Runner(None)
    c = make_client(runner)
    runner.root = c.output
    wait_for(c, c.post("/api/jobs", json={"jd": "Node.js engineer", "gaps": False}, headers=OK).json()["id"])
    wait_for(c, c.post("/api/jobs", json={"jd": "Node.js engineer"}, headers=OK).json()["id"])
    assert [call[4] for call in runner.calls] == [False, True]  # unticked box -> no gap report; default on
    page = c.get("/", headers={"Origin": "null"}).text
    assert '<input type="checkbox" id="gaps" checked> Generate gap report' in page
