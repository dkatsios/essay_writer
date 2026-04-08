"""FastAPI web UI for the essay writer pipeline."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader

load_dotenv()

from config.schemas import load_config  # noqa: E402
from src.agent import create_model  # noqa: E402
from src.intake import build_extracted_text, scan  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402
from src.runner import TokenTracker, _StepTimer, _make_callbacks  # noqa: E402
from src.runner import _parse_validation_answers  # noqa: E402
from src.schemas import AssignmentBrief, Clarification, ValidationQuestion  # noqa: E402

logger = logging.getLogger(__name__)

app = FastAPI(title="Essay Writer")

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
    status: str = "running"  # running | questions | done | error
    run_dir: Path = field(default_factory=lambda: Path("."))
    questions: list[dict] | None = None
    answers_event: threading.Event = field(default_factory=threading.Event)
    answers: str = ""
    error: str = ""
    academic_level: str = ""
    tracker: TokenTracker | None = None


_jobs: dict[str, Job] = {}


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
                {"question": q.question, "options": q.options} for q in remaining
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

    except Exception:
        logger.exception("Pipeline failed for job %s", job.job_id)
        job.status = "error"
        job.error = "Pipeline failed. Check server logs for details."


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
    files: list[UploadFile] = [],  # noqa: B006
    sources: list[UploadFile] = [],  # noqa: B006
):
    """Accept form data, start the pipeline in a background thread."""
    job_id = uuid.uuid4().hex[:12]

    run_dir = Path(tempfile.mkdtemp(prefix=f"essay_{job_id}_"))
    job = Job(job_id=job_id, run_dir=run_dir, academic_level=academic_level)
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
    if job.status == "error":
        resp["error"] = job.error
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
    job.status = "running"
    job.answers_event.set()
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
