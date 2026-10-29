"""Local API + page server for the CV pipeline. Listens on 127.0.0.1 only.

    python -m cvgen.server [--port 8765] [--log-file logs/server.log]     (or: cvgen serve)

Endpoints (all /api ones except /api/health need the X-CVGen-Token header):
    GET  /                          the CV Generator page (with the port and token filled in)
    GET  /api/health                {"ok": true}
    POST /api/jobs                  {jd, company?, role?, pages?, gaps?} -> {"id": ...}; queued, run one at a time
    GET  /api/jobs                  today's jobs
    GET  /api/jobs/{id}             stage, and when done: folder, file links, layout, cost, gap report
    POST /api/jobs/{id}/open-folder opens the output folder in Explorer/Finder/the file manager
    POST /api/shutdown              stops the server (used by `cvgen service uninstall`)
    GET  /files/<folder>/<file>     a file from output/ (no path traversal)

Security: the server spends API credits, so
- requests must be addressed to 127.0.0.1/localhost on our port (Host check, against DNS rebinding);
- a request with an Origin header must come from "null" (a file:// page) or http://127.0.0.1:<port>,
  and every POST must carry one of those;
- every /api call except /api/health must send the local access token from .cvgen_token. "null" is
  also what sandboxed iframes on any website send, so the Origin check alone isn't enough. The token
  is written into the desktop copy of the page and into the page served at "/"; neither can be read
  by another site (no CORS on them). It is not the Anthropic API key, which never leaves this process.
"""

import argparse
import json
import logging
import logging.handlers
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from cvgen import render as _render
from cvgen.pipeline import ANON_BASENAME, DEFAULT_OUTPUT_ROOT, REPO_ROOT

DEFAULT_PORT = 8765
UI_FILE = REPO_ROOT / "ui" / "CV Generator.html"
TOKEN_FILE = REPO_ROOT / ".cvgen_token"
LOG_FILE = REPO_ROOT / "logs" / "server.log"
TOKEN_HEADER = "X-CVGen-Token"
MAX_JD_CHARS = 60_000
_CONFIG = re.compile(r"/\*CVGEN-CONFIG\*/.*?/\*END-CVGEN-CONFIG\*/", re.S)

log = logging.getLogger("cvgen.server")


# ---------------------------
# SETTINGS AND TOKEN
# ---------------------------
def configured_port() -> int:
    load_dotenv(REPO_ROOT / ".env")
    return int(os.environ.get("CVGEN_PORT") or DEFAULT_PORT)


def load_token(path: Path = TOKEN_FILE) -> str:
    """The local access token (created on first use). Not the Anthropic API key."""
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def page_html(port: int, token: str, ui_file: Path = UI_FILE) -> str:
    """The UI page with this server's port and token filled into its config line."""
    html = ui_file.read_text(encoding="utf-8")
    config = json.dumps({"port": port, "token": token})
    return _CONFIG.sub(lambda m: f"/*CVGEN-CONFIG*/{config}/*END-CVGEN-CONFIG*/", html, count=1)


def scrub(text: str) -> str:
    """Remove the API key from any text that might reach a response or a log."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        secret = os.environ.get(name)
        if secret and len(secret) > 8:
            text = text.replace(secret, "***")
    return text


# ---------------------------
# JOBS
# ---------------------------
def stage_for(message: str) -> str | None:
    """Map a pipeline progress message to a stage name, or None to keep the current stage."""
    if message.startswith("Tailoring"):
        return "tailoring"
    if message.startswith(("Validating", "Repairing ")) and "gap report" not in message:
        return "validating"
    if message.startswith(("Rendering", "Layout check")):
        return "rendering"
    m = re.match(r"Layout round (\d+)/(\d+)", message)
    if m:
        return f"layout round {m[1]}/{m[2]}"
    if message.startswith(("Gap report", "Repairing the gap report")):
        return "gap report"
    return None


@dataclass
class Job:
    id: str
    created: str
    title: str  # first line of the JD
    company: str | None
    role: str | None
    pages: int | None
    gaps: bool
    status: str = "queued"  # queued, running, done, failed
    stage: str = "queued"  # queued, tailoring, validating, rendering, layout round n/m, gap report, done, failed
    message: str = ""
    started: str | None = None
    finished: str | None = None
    folder: str | None = None
    error: str | None = None
    summary: dict = field(default_factory=dict)  # company, role, fit, pages, widows, cost

    def public(self) -> dict:
        return asdict(self)


class JobQueue:
    """Runs jobs one at a time on a worker thread; keeps them in output/.jobs.json across restarts."""

    def __init__(self, output_root: Path, runner=None, store: Path | None = None):
        self.output_root = Path(output_root)
        self.store = store or self.output_root / ".jobs.json"
        self.runner = runner  # callable(jd, company, role, pages, gaps, progress) -> folder Path
        self.jobs: dict[str, Job] = {}
        self.jds: dict[str, str] = {}
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self._load()
        self.thread = threading.Thread(target=self._worker, name="cvgen-jobs", daemon=True)
        self.thread.start()

    def _load(self) -> None:
        if not self.store.is_file():
            return
        try:
            for data in json.loads(self.store.read_text(encoding="utf-8")):
                job = Job(**data)
                if job.status in ("queued", "running"):  # interrupted by a restart
                    job.status = job.stage = "failed"
                    job.error = "The server restarted before this job finished. Submit it again."
                self.jobs[job.id] = job
        except (ValueError, TypeError) as exc:
            log.warning("ignoring unreadable %s: %s", self.store, exc)

    def _save(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        recent = sorted(self.jobs.values(), key=lambda j: j.created)[-200:]
        self.store.write_text(json.dumps([j.public() for j in recent], ensure_ascii=False, indent=1), encoding="utf-8")

    def submit(self, jd: str, company=None, role=None, pages=None, gaps=True) -> Job:
        title = next((line.strip() for line in jd.splitlines() if line.strip()), "Job description")[:120]
        job = Job(id=uuid.uuid4().hex[:12], created=datetime.now().isoformat(timespec="seconds"), title=title,
                  company=company or None, role=role or None, pages=pages, gaps=gaps)
        with self.wake:
            self.jobs[job.id] = job
            self.jds[job.id] = jd
            self._save()
            self.wake.notify()
        log.info("queued job %s: %s", job.id, title)
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def today(self) -> list[Job]:
        prefix = date.today().isoformat()
        with self.lock:
            return sorted((j for j in self.jobs.values() if j.created.startswith(prefix)), key=lambda j: j.created, reverse=True)

    def _next(self) -> Job:
        with self.wake:
            while True:
                queued = sorted((j for j in self.jobs.values() if j.status == "queued" and j.id in self.jds), key=lambda j: j.created)
                if queued:
                    job = queued[0]
                    job.status, job.stage, job.started = "running", "starting", datetime.now().isoformat(timespec="seconds")
                    self._save()
                    return job
                self.wake.wait()

    def _worker(self) -> None:
        while True:
            job = self._next()

            def progress(message: str, job=job) -> None:
                stage = stage_for(message)
                with self.lock:
                    job.message = scrub(message)
                    if stage:
                        job.stage = stage
                log.info("job %s: %s", job.id, message)

            try:
                folder = self.runner(self.jds[job.id], job.company, job.role, job.pages, job.gaps, progress)
                summary = summarize(Path(folder))
                with self.lock:
                    job.status = job.stage = "done"
                    job.folder, job.summary, job.message = Path(folder).name, summary, ""
            except Exception as exc:  # report every failure on the job; keep the worker alive
                from cvgen.cli import explain
                log.exception("job %s failed", job.id)
                with self.lock:
                    job.status = job.stage = "failed"
                    job.error = scrub(explain(exc))
            finally:
                with self.lock:
                    job.finished = datetime.now().isoformat(timespec="seconds")
                    self.jds.pop(job.id, None)
                    self._save()


def summarize(folder: Path) -> dict:
    meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
    lay = meta.get("layout", {})
    gap = meta.get("gap_report") or {}
    return {"company": meta.get("company"), "role": meta.get("role"), "fit": gap.get("fit"),
            "gap_error": gap.get("error"), "pages": lay.get("final_pages"), "widows": lay.get("final_widows"),
            "cost_usd": meta.get("cost_usd"), "model": meta.get("model")}


def default_runner(jd, company, role, pages, gaps, progress) -> Path:
    from cvgen.pipeline import run
    return run(jd, company, role, pages=pages, gaps=gaps, progress=progress)


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 (a folder we created, not user input)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------
# APP
# ---------------------------
def create_app(*, port: int = DEFAULT_PORT, token: str | None = None, output_root: Path = DEFAULT_OUTPUT_ROOT,
               runner=None, ui_file: Path = UI_FILE, opener=open_in_file_manager):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from pydantic import BaseModel, Field

    token = token or load_token()
    output_root = Path(output_root).resolve()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {"null", f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    queue = JobQueue(output_root, runner or default_runner)
    app = FastAPI(title="CV Generator", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.queue = queue
    app.state.server = None  # set by main(), so /api/shutdown can stop uvicorn

    def cors(response, origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        return response

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path, method = request.url.path, request.method
        origin = request.headers.get("origin")
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"error": "Wrong Host header."}, status_code=403)
        if origin is not None and origin not in allowed_origins:
            return JSONResponse({"error": "Origin not allowed."}, status_code=403)
        api = path.startswith("/api/")
        if method == "OPTIONS":  # CORS preflight, only for the API
            if not (api and origin):
                return Response(status_code=403)
            return cors(Response(status_code=204, headers={
                "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": f"Content-Type, {TOKEN_HEADER}",
                "Access-Control-Max-Age": "600"}), origin)
        if method not in ("GET", "HEAD") and origin is None:
            return JSONResponse({"error": "Missing Origin header."}, status_code=403)
        if api and path != "/api/health" and not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            response = JSONResponse({"error": "Missing or wrong access token. Open the page from your desktop "
                                              f"copy or http://127.0.0.1:{port}/."}, status_code=403)
            return cors(response, origin) if origin else response
        response = await call_next(request)
        return cors(response, origin) if (api and origin) else response  # never CORS on "/" or /files

    class NewJob(BaseModel):
        jd: str = Field(min_length=1, max_length=MAX_JD_CHARS)
        company: str | None = Field(default=None, max_length=200)
        role: str | None = Field(default=None, max_length=200)
        pages: int | None = Field(default=None, ge=1, le=2)
        gaps: bool = True

    @app.get("/", response_class=HTMLResponse)
    def page():
        return HTMLResponse(page_html(port, token, ui_file), headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/jobs")
    def create_job(body: NewJob):
        if not body.jd.strip():
            raise HTTPException(422, "The job description is empty.")
        job = queue.submit(body.jd, body.company, body.role, body.pages, body.gaps)
        return {"id": job.id, "status": job.status}

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": [j.public() for j in queue.today()]}

    def folder_of(job) -> Path:
        folder = (output_root / job.folder).resolve()
        if folder.parent != output_root or not folder.is_dir():
            raise HTTPException(404, "Output folder not found.")
        return folder

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = queue.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        data = job.public()
        if job.status == "done" and job.folder:
            folder = folder_of(job)
            files = {p.name: f"/files/{job.folder}/{p.name}" for p in sorted(folder.iterdir()) if p.is_file()}
            data["files"] = files
            anon = ANON_BASENAME
            pick = lambda ext, want_anon: next((u for n, u in files.items() if n.startswith("CV_") and n.endswith(ext)
                                                and (n == anon + ext) == want_anon), None)
            data["pdf"], data["docx"] = pick(".pdf", False), pick(".docx", False)
            data["anon_pdf"], data["anon_docx"] = pick(".pdf", True), pick(".docx", True)
            report = folder / "gap_report.json"
            data["gap_report"] = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None
            meta = json.loads((folder / "cv.meta.json").read_text(encoding="utf-8"))
            data["layout"] = {k: meta.get("layout", {}).get(k) for k in ("final_pages", "final_widows", "kept_round")}
            data["cv_gaps"] = meta.get("gaps", [])
        return data

    @app.post("/api/jobs/{job_id}/open-folder")
    def open_folder(job_id: str):
        job = queue.get(job_id)
        if job is None or not job.folder:
            raise HTTPException(404, "That job has no output folder yet.")
        opener(folder_of(job))
        return {"ok": True}

    @app.post("/api/shutdown")
    def shutdown():
        if app.state.server is not None:
            app.state.server.should_exit = True
        return {"ok": True}

    @app.get("/files/{folder}/{name}")
    def files(folder: str, name: str):
        target = (output_root / folder / name).resolve()
        if target.parent.parent != output_root or name.startswith(".") or not target.is_file():
            raise HTTPException(404, "Not found.")
        return FileResponse(target)

    return app


# ---------------------------
# ENTRY POINT
# ---------------------------
def setup_logging(log_file: Path | None) -> None:
    if sys.stdout is None:  # pythonw: no console
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers = [logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True)
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv=None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(prog="python -m cvgen.server", description="Local server for the CV Generator page.")
    parser.add_argument("--port", type=int, default=None, help=f"default: CVGEN_PORT or {DEFAULT_PORT}")
    parser.add_argument("--log-file", type=Path, default=None, help="log here instead of the console")
    args = parser.parse_args(argv)
    setup_logging(args.log_file)
    port = args.port or configured_port()
    if sys.platform == "win32":
        _render.SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW  # no console flash for LibreOffice
    app = create_app(port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    app.state.server = server
    log.info("CV Generator server on http://127.0.0.1:%d/ (output: %s)", port, DEFAULT_OUTPUT_ROOT)
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
