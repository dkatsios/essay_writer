"""FastAPI web UI for the essay writer pipeline."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader

load_dotenv()

from config.schemas import load_config  # noqa: E402
from src.agent import create_model  # noqa: E402
from src.intake import build_extracted_text, scan  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402
from src.tools._http import http_get  # noqa: E402
from src.tools.web_fetcher import extract_pdf_bytes_to_text  # noqa: E402
from src.runner import TokenTracker, _StepTimer, _make_callbacks  # noqa: E402
from src.runner import _parse_validation_answers  # noqa: E402
from src.schemas import AssignmentBrief, Clarification, ValidationQuestion  # noqa: E402

logger = logging.getLogger(__name__)

# Remove completed/failed web jobs (and temp dirs) if never downloaded.
_DEFAULT_JOB_TTL_SECONDS = 86_400
_DEFAULT_SWEEP_INTERVAL_SECONDS = 300

_jinja_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates" / "web"),
    autoescape=True,
)

# Keywords (Greek + English) that indicate an academic-level question
_LEVEL_KEYWORDS = {
    "ακαδημαϊκό επίπεδο",
    "ακαδημαικό επίπεδο",
    "academic level",
    "προπτυχιακό",
    "μεταπτυχιακό",
    "επίπεδο σπουδών",
    "επιπεδο σπουδων",
}
# Pre-normalize keywords to NFC
_LEVEL_KEYWORDS_NFC = {unicodedata.normalize("NFC", kw) for kw in _LEVEL_KEYWORDS}


def _is_academic_level_question(q: ValidationQuestion) -> bool:
    text = unicodedata.normalize("NFC", q.question.lower())
    return any(kw in text for kw in _LEVEL_KEYWORDS_NFC)


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """In-memory state for a single pipeline run."""

    job_id: str
    status: str = "running"  # running | questions | optional_pdfs | done | error
    run_dir: Path = field(default_factory=lambda: Path("."))
    questions: list[dict] | None = None
    answers_event: threading.Event = field(default_factory=threading.Event)
    answers: str = ""
    optional_pdf_items: list[dict] | None = None
    optional_pdf_allowed_ids: frozenset[str] | None = None
    optional_pdf_event: threading.Event = field(default_factory=threading.Event)
    error: str = ""
    academic_level: str = ""
    submit_prompt: str = ""
    target_words: int | None = None
    min_sources: int | None = None
    tracker: TokenTracker | None = None
    created_at: float = field(default_factory=time.time)
    """Wall time when the job was submitted."""
    finished_at: float | None = None
    """Wall time when status became ``done`` or ``error``."""
    clarification_rounds: list[dict] = field(default_factory=list)
    """Each ``{"items": [{"question": str, "answer": str}, ...]}`` for UI replay after reload."""
    optional_pdf_rounds: list[dict] = field(default_factory=list)
    """Each ``{"items": [{"title": str, "answer": str}, ...]}`` for optional-PDF step replay."""
    optional_pdf_choices: dict[str, str] = field(default_factory=dict)
    """``source_id -> \"file\"|\"url\"`` for the current optional-PDF step."""
    fast_track: bool = False
    """If True, do not pause for the optional full-text PDF upload step."""

_jobs: dict[str, Job] = {}


def _append_clarification_round_for_ui(job: Job, answers: str) -> None:
    """Record Q&A for the current ``job.questions`` so the web UI can restore history after reload."""
    if not job.questions:
        return
    vqs = [
        ValidationQuestion(
            question=q["question"],
            options=q["options"],
            suggested_option_index=int(q.get("suggested_option_index", 0)),
        )
        for q in job.questions
    ]
    parsed = (
        _parse_validation_answers(vqs, answers) if answers.strip() else []
    )
    by_q = {c.question: c.answer for c in parsed}
    items = [
        {"question": q["question"], "answer": by_q.get(q["question"], "—")}
        for q in job.questions
    ]
    job.clarification_rounds.append({"items": items})


def _append_optional_pdf_round_for_ui(job: Job) -> None:
    """Snapshot optional-PDF choices for the current ``job.optional_pdf_items``."""
    items = job.optional_pdf_items or []
    round_items: list[dict] = []
    for row in items:
        sid = str(row["source_id"])
        how = job.optional_pdf_choices.get(sid)
        if how == "file":
            ans = "PDF from file"
        elif how == "url":
            ans = "PDF from URL"
        else:
            ans = "— skipped / none"
        title = row.get("title") or sid
        round_items.append({"title": str(title), "answer": ans})
    job.optional_pdf_rounds.append({"items": round_items})
    job.optional_pdf_choices.clear()


def _job_ttl_seconds() -> int:
    raw = os.environ.get(
        "ESSAY_WEB_JOB_TTL_SECONDS", str(_DEFAULT_JOB_TTL_SECONDS)
    ).strip()
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_JOB_TTL_SECONDS
    return max(0, n)


def _job_sweep_interval_seconds() -> int:
    raw = os.environ.get(
        "ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS", str(_DEFAULT_SWEEP_INTERVAL_SECONDS)
    ).strip()
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_SWEEP_INTERVAL_SECONDS
    return max(60, n)


def job_ttl_sweep_once(now: float | None = None) -> int:
    """Remove ``done`` / ``error`` jobs whose ``finished_at`` is older than TTL.

    Returns the number of jobs removed. No-op if ``ESSAY_WEB_JOB_TTL_SECONDS``
    is ``0`` (disabled).
    """
    ttl = _job_ttl_seconds()
    if ttl <= 0:
        return 0
    t = time.time() if now is None else now
    removed = 0
    for jid, job in list(_jobs.items()):
        if job.status not in ("done", "error"):
            continue
        if job.finished_at is None:
            continue
        if t - job.finished_at <= ttl:
            continue
        _jobs.pop(jid, None)
        shutil.rmtree(job.run_dir, ignore_errors=True)
        removed += 1
        logger.info(
            "TTL cleanup removed job %s (status=%s, age=%.0fs)",
            jid,
            job.status,
            t - job.finished_at,
        )
    return removed


def _job_ttl_sweeper_loop() -> None:
    interval = _job_sweep_interval_seconds()
    while True:
        time.sleep(interval)
        try:
            job_ttl_sweep_once()
        except Exception:
            logger.exception("Job TTL sweep failed")


def _start_job_ttl_sweeper() -> None:
    if _job_ttl_seconds() <= 0:
        logger.info("ESSAY_WEB_JOB_TTL_SECONDS is 0; stale-job sweeper disabled")
        return
    t = threading.Thread(
        target=_job_ttl_sweeper_loop,
        name="essay-job-ttl-sweeper",
        daemon=True,
    )
    t.start()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _start_job_ttl_sweeper()
    yield


app = FastAPI(title="Essay Writer", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Pipeline thread
# ---------------------------------------------------------------------------


def _run_pipeline_thread(
    job: Job,
    upload_dir: Path | None,
    prompt: str | None,
    min_sources: int | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Execute the essay pipeline in a background thread."""
    try:
        config = load_config()

        run_dir = job.run_dir
        input_dir = run_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        if upload_dir and any(upload_dir.iterdir()):
            input_files = scan(str(upload_dir))
            extracted_text = build_extracted_text(input_files, extra_prompt=prompt)
            del input_files
        elif prompt:
            extracted_text = f"# Assignment\n\n{prompt}\n"
        else:
            job.status = "error"
            job.error = "Provide at least a prompt or upload files."
            job.finished_at = time.time()
            return

        (input_dir / "extracted.md").write_text(extracted_text, encoding="utf-8")

        worker = create_model(config.models.worker)
        writer = create_model(config.models.writer)
        reviewer = create_model(config.models.reviewer)

        timer = _StepTimer()
        tracker = TokenTracker()
        job.tracker = tracker
        callbacks = _make_callbacks(timer, tracker)

        def _on_questions(questions: list[ValidationQuestion], rd: Path) -> None:
            # Auto-answer academic level question if user already chose one
            remaining = questions
            auto_clarifications: list = []
            if job.academic_level:
                remaining = []
                for q in questions:
                    if _is_academic_level_question(q):
                        auto_clarifications.append(
                            Clarification(
                                question=q.question,
                                answer=job.academic_level,
                            )
                        )
                    else:
                        remaining.append(q)

            # Apply auto-answers immediately
            if auto_clarifications:
                brief_path = rd / "brief" / "assignment.json"
                brief = AssignmentBrief.model_validate_json(
                    brief_path.read_text(encoding="utf-8")
                )
                if brief.clarifications is None:
                    brief.clarifications = []
                brief.clarifications.extend(auto_clarifications)
                if not brief.academic_level:
                    brief.academic_level = job.academic_level
                brief_path.write_text(
                    brief.model_dump_json(indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            # If no remaining questions, skip user interaction
            if not remaining:
                return

            job.questions = [
                {
                    "question": q.question,
                    "options": q.options,
                    "suggested_option_index": q.suggested_option_index,
                }
                for q in remaining
            ]
            job.status = "questions"
            job.answers_event.clear()
            # Block until web user submits answers
            job.answers_event.wait()

            if not job.answers:
                return

            brief_path = rd / "brief" / "assignment.json"
            brief = AssignmentBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8")
            )
            if brief.clarifications is None:
                brief.clarifications = []
            brief.clarifications.extend(
                _parse_validation_answers(remaining, job.answers)
            )
            brief_path.write_text(
                brief.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
            )

        def _on_optional_pdfs(rd: Path, items: list[dict]) -> None:
            if not items:
                return
            if job.fast_track:
                return
            job.optional_pdf_choices.clear()
            job.optional_pdf_items = items
            job.optional_pdf_allowed_ids = frozenset(
                str(row["source_id"]) for row in items
            )
            job.status = "optional_pdfs"
            job.optional_pdf_event.clear()
            job.optional_pdf_event.wait()
            job.optional_pdf_items = None
            job.optional_pdf_allowed_ids = None

        run_pipeline(
            worker,
            writer,
            reviewer,
            run_dir,
            config,
            extra_prompt=prompt,
            callbacks=callbacks,
            token_tracker=tracker,
            on_questions=_on_questions,
            on_optional_source_pdfs=_on_optional_pdfs,
            min_sources=min_sources,
            user_sources_dir=user_sources_dir,
        )

        # Copy docx into run_dir if needed
        docx_src = Path(config.paths.output_dir) / "essay.docx"
        docx_dest = run_dir / "essay.docx"
        if docx_src.exists() and not docx_dest.exists():
            shutil.copy2(str(docx_src), str(docx_dest))

        tracker.write_report(run_dir)
        job.status = "done"
        job.finished_at = time.time()

    except Exception:
        logger.exception("Pipeline failed for job %s", job.job_id)
        job.status = "error"
        job.error = "Pipeline failed. Check server logs for details."
        job.finished_at = time.time()


# ---------------------------------------------------------------------------
# Zip builder
# ---------------------------------------------------------------------------


def _build_zip(run_dir: Path) -> BytesIO:
    """Zip the full run directory tree (same layout as ``.output/run_*`` from the CLI)."""
    buf = BytesIO()
    root = run_dir.resolve()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(root).as_posix()
                zf.write(path, arcname)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Liveness probe for platforms (e.g. Render) — no API keys required."""
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page form."""
    template = _jinja_env.get_template("index.html")
    return template.render()


@app.post("/submit")
async def submit(
    prompt: str = Form(""),
    target_words: int | None = Form(None),
    min_sources: int | None = Form(None),
    academic_level: str = Form(""),
    fast_track: str | None = Form(None),
    files: list[UploadFile] = [],  # noqa: B006
    sources: list[UploadFile] = [],  # noqa: B006
):
    """Accept form data, start the pipeline in a background thread."""
    job_id = uuid.uuid4().hex[:12]

    run_dir = Path(tempfile.mkdtemp(prefix=f"essay_{job_id}_"))
    tw = target_words if target_words is not None and target_words > 0 else None
    ms = min_sources if min_sources is not None and min_sources > 0 else None
    job = Job(
        job_id=job_id,
        run_dir=run_dir,
        academic_level=academic_level.strip(),
        submit_prompt=prompt.strip(),
        target_words=tw,
        min_sources=ms,
        fast_track=bool(fast_track and fast_track.strip().lower() in ("1", "on", "true", "yes")),
    )
    _jobs[job_id] = job

    # Save uploaded files
    upload_dir: Path | None = None
    if files and files[0].filename:
        upload_dir = run_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.filename:
                dest = upload_dir / Path(f.filename).name
                content = await f.read()
                dest.write_bytes(content)

    # Save user-provided source files
    user_sources_dir: Path | None = None
    if sources and sources[0].filename:
        user_sources_dir = run_dir / "user_sources"
        user_sources_dir.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(sources):
            if f.filename:
                dest = user_sources_dir / f"{i:03d}_{Path(f.filename).name}"
                content = await f.read()
                dest.write_bytes(content)

    # Build extra_prompt with optional word target and academic level
    extra_prompt = prompt.strip() or None
    if target_words and target_words > 0:
        words_line = f"Target word count: {target_words} words."
        extra_prompt = f"{words_line}\n{extra_prompt}" if extra_prompt else words_line
    if academic_level:
        level_line = f"Academic level: {academic_level}."
        extra_prompt = f"{level_line}\n{extra_prompt}" if extra_prompt else level_line

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(job, upload_dir, extra_prompt, min_sources, user_sources_dir),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"job_id": job_id})


@app.get("/status/{job_id}")
async def status(job_id: str):
    """Poll job status."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    resp: dict = {"status": job.status}
    if job.status == "running" and job.tracker is not None:
        resp["stage"] = job.tracker.get_current_step()
    if job.status == "questions" and job.questions:
        resp["questions"] = job.questions
    if job.status == "optional_pdfs" and job.optional_pdf_items:
        resp["optional_pdf_items"] = job.optional_pdf_items
    if job.status == "error":
        resp["error"] = job.error
    resp["clarification_rounds"] = job.clarification_rounds
    resp["optional_pdf_rounds"] = job.optional_pdf_rounds
    resp["submit"] = {
        "prompt": job.submit_prompt,
        "academic_level": job.academic_level,
        "target_words": job.target_words,
        "min_sources": job.min_sources,
    }
    return JSONResponse(resp)


@app.post("/answer/{job_id}")
async def answer(job_id: str, answers: str = Form("")):
    """Submit answers to validation questions, unblock the pipeline thread."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "questions":
        return JSONResponse({"error": "No pending questions"}, status_code=400)

    job.answers = answers
    _append_clarification_round_for_ui(job, answers)
    job.status = "running"
    job.answers_event.set()
    return JSONResponse({"status": "ok"})


_MAX_OPTIONAL_PDF_BYTES = 30 * 1024 * 1024


def _fetch_pdf_bytes_from_url(url: str) -> tuple[bytes | None, str | None]:
    """Download PDF bytes from http(s) URL. Returns (data, error_message)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "Invalid URL (use http or https)"
    try:
        resp = http_get(
            url,
            follow_redirects=True,
            max_retries=2,
            initial_backoff=1.0,
            request_name="optional pdf url",
        )
    except httpx.HTTPError as exc:
        logger.warning("Optional PDF URL fetch failed: %s", exc)
        return None, "Could not download URL"
    raw = resp.content
    if len(raw) > _MAX_OPTIONAL_PDF_BYTES:
        return None, "File too large"
    if not raw.startswith(b"%PDF"):
        return None, "URL did not return a PDF"
    return raw, None


def _apply_optional_pdf_bytes(job: Job, job_id: str, sid: str, raw: bytes) -> str | None:
    """Persist extracted text to supplement + registry. Returns error message or None."""
    if len(raw) > _MAX_OPTIONAL_PDF_BYTES:
        return "File too large"
    if not raw.startswith(b"%PDF"):
        return "Not a valid PDF"
    try:
        text = extract_pdf_bytes_to_text(raw)
    except Exception:
        logger.exception("PDF extract failed for job %s source %s", job_id, sid)
        return "Could not read PDF text"
    if len(text) > 50_000:
        text = text[:50_000] + "\n\n[... truncated ...]"

    run_dir = job.run_dir
    supplement = run_dir / "sources" / "supplement"
    supplement.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:120]
    txt_path = supplement / f"{safe}.txt"
    txt_path.write_text(text, encoding="utf-8")

    reg_path = run_dir / "sources" / "registry.json"
    registry = json.loads(reg_path.read_text(encoding="utf-8"))
    if sid not in registry:
        return "Source not in registry"
    registry[sid]["content_path"] = str(txt_path.resolve())
    reg_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return None


@app.post("/optional-pdf/{job_id}")
async def optional_pdf_upload(
    job_id: str,
    source_id: str = Form(""),
    pdf_url: str = Form(""),
    file: UploadFile | None = None,
):
    """Attach a user PDF (file upload or http(s) URL) to a registry source."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "optional_pdfs":
        return JSONResponse({"error": "No optional PDF step active"}, status_code=400)
    sid = source_id.strip()
    if not sid or job.optional_pdf_allowed_ids is None or sid not in job.optional_pdf_allowed_ids:
        return JSONResponse({"error": "Invalid source_id"}, status_code=400)

    url_stripped = pdf_url.strip()
    raw: bytes | None = None
    if url_stripped:
        raw, err = _fetch_pdf_bytes_from_url(url_stripped)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    elif file is not None and file.filename:
        if not file.filename.lower().endswith(".pdf"):
            return JSONResponse({"error": "Only PDF files are accepted"}, status_code=400)
        raw = await file.read()
    else:
        return JSONResponse(
            {"error": "Provide a PDF file or a PDF URL"}, status_code=400
        )

    err = _apply_optional_pdf_bytes(job, job_id, sid, raw)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    job.optional_pdf_choices[sid] = "url" if url_stripped else "file"
    return JSONResponse({"status": "ok", "source_id": sid})


@app.post("/optional-pdf/{job_id}/done")
async def optional_pdf_done(job_id: str):
    """Continue the pipeline after optional PDF uploads (or skip)."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "optional_pdfs":
        return JSONResponse({"error": "No optional PDF step active"}, status_code=400)

    _append_optional_pdf_round_for_ui(job)
    job.status = "running"
    job.optional_pdf_event.set()
    return JSONResponse({"status": "ok"})


@app.get("/download/{job_id}")
async def download(job_id: str):
    """Return the result zip, then remove the job's run directory and job record."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "done":
        return JSONResponse({"error": "Job not ready"}, status_code=400)

    run_dir = job.run_dir
    buf = _build_zip(run_dir)
    chunk_size = 64 * 1024

    def iter_zip():
        try:
            while True:
                chunk = buf.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            buf.close()
            try:
                shutil.rmtree(run_dir, ignore_errors=True)
            finally:
                _jobs.pop(job_id, None)

    return StreamingResponse(
        iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=essay_{job_id}.zip"},
    )
