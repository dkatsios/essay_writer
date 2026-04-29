"""FastAPI web UI for the essay writer pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader

load_dotenv()

from config.settings import ModelsConfig, PROVIDER_PRESETS, load_config  # noqa: E402
from src import web_jobs  # noqa: E402
from src.agent import create_async_client, create_client  # noqa: E402
from src.intake import build_extracted_text, scan  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402
from src.run_logging import configure_web_logging, run_id_context  # noqa: E402
from src.runtime import parse_validation_answers  # noqa: E402
from src.tools._http import close_http_clients  # noqa: E402

logger = logging.getLogger(__name__)

_jinja_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates" / "web"),
    autoescape=True,
)

Job = web_jobs.Job
_JobInteractionTimeout = web_jobs.JobInteractionTimeout
_jobs = web_jobs.jobs
_notify_job = web_jobs.notify_job
_build_status_payload = web_jobs.build_status_payload
job_ttl_sweep_once = web_jobs.job_ttl_sweep_once


async def _run_pipeline_task(
    job: Job,
    upload_dir: Path | None,
    prompt: str | None,
    min_sources: int | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    await web_jobs.run_pipeline_task(
        job,
        upload_dir,
        prompt,
        min_sources,
        user_sources_dir,
        load_config_fn=load_config,
        models_config_cls=ModelsConfig,
        create_client_fn=create_client,
        create_async_client_fn=create_async_client,
        run_pipeline_fn=run_pipeline,
        scan_fn=scan,
        build_extracted_text_fn=build_extracted_text,
        parse_validation_answers_fn=parse_validation_answers,
        is_academic_level_question_fn=web_jobs.is_academic_level_question,
        interaction_timeout_seconds_fn=web_jobs.interaction_timeout_seconds,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_web_logging()
    logger.info("Web application logging configured")
    web_jobs.start_job_ttl_sweeper()
    yield
    close_http_clients()


app = FastAPI(title="Essay Writer", lifespan=_lifespan)


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
    provider: str = Form(""),
    api_key: str = Form(""),
    fast_track: str | None = Form(None),
    files: list[UploadFile] = [],  # noqa: B006
    sources: list[UploadFile] = [],  # noqa: B006
):
    """Accept form data, start the pipeline as a background task."""
    job_id = uuid.uuid4().hex[:12]

    run_dir = Path(tempfile.mkdtemp(prefix=f"essay_{job_id}_"))
    target_words_value = (
        target_words if target_words is not None and target_words > 0 else None
    )
    min_sources_value = (
        min_sources if min_sources is not None and min_sources > 0 else None
    )
    provider_value = provider.strip().lower()
    if provider_value and provider_value not in PROVIDER_PRESETS:
        return JSONResponse(
            {
                "error": f"Unknown provider {provider_value!r}. Choose from: {', '.join(sorted(PROVIDER_PRESETS))}."
            },
            status_code=400,
        )

    job = Job(
        job_id=job_id,
        run_dir=run_dir,
        academic_level=academic_level.strip(),
        submit_prompt=prompt.strip(),
        target_words=target_words_value,
        min_sources=min_sources_value,
        fast_track=bool(
            fast_track and fast_track.strip().lower() in ("1", "on", "true", "yes")
        ),
        provider=provider_value,
        api_key=api_key.strip(),
    )
    _jobs[job_id] = job

    with run_id_context(job_id):
        logger.info(
            "Job %s submitted (provider=%s, target_words=%s, min_sources=%s, files=%d, sources=%d)",
            job_id,
            provider_value or "default",
            target_words_value,
            min_sources_value,
            len([file for file in files if file.filename]),
            len([file for file in sources if file.filename]),
        )

    upload_dir: Path | None = None
    if files and files[0].filename:
        upload_dir = run_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        for file in files:
            if file.filename:
                destination = upload_dir / Path(file.filename).name
                destination.write_bytes(await file.read())

    user_sources_dir: Path | None = None
    if sources and sources[0].filename:
        user_sources_dir = run_dir / "user_sources"
        user_sources_dir.mkdir(parents=True, exist_ok=True)
        for index, file in enumerate(sources):
            if file.filename:
                destination = (
                    user_sources_dir / f"{index:03d}_{Path(file.filename).name}"
                )
                destination.write_bytes(await file.read())

    extra_prompt = prompt.strip() or None
    if target_words and target_words > 0:
        words_line = f"Target word count: {target_words} words."
        extra_prompt = f"{words_line}\n{extra_prompt}" if extra_prompt else words_line
    if academic_level:
        level_line = f"Academic level: {academic_level}."
        extra_prompt = f"{level_line}\n{extra_prompt}" if extra_prompt else level_line

    asyncio.create_task(
        _run_pipeline_task(
            job, upload_dir, extra_prompt, min_sources_value, user_sources_dir
        )
    )

    with run_id_context(job_id):
        logger.info("Job %s background task started", job_id)

    return JSONResponse({"job_id": job_id})


_SSE_POLL_INTERVAL = 2.0


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    """Server-Sent Events stream for real-time job status updates."""
    job = _jobs.get(job_id)

    async def generate():
        if job is None:
            yield f"data: {json.dumps({'status': 'gone'})}\n\n"
            return

        last_payload_json: str | None = None
        payload = _build_status_payload(job)
        last_payload_json = json.dumps(payload, ensure_ascii=False)
        yield f"data: {last_payload_json}\n\n"

        if job.status in ("done", "error"):
            return

        while True:
            try:
                await asyncio.wait_for(
                    job._sse_event.wait(), timeout=_SSE_POLL_INTERVAL
                )
            except TimeoutError:
                pass
            job._sse_event.clear()

            if job_id not in _jobs:
                yield f"data: {json.dumps({'status': 'gone'})}\n\n"
                return

            payload = _build_status_payload(job)
            payload_json = json.dumps(payload, ensure_ascii=False)
            if payload_json != last_payload_json:
                last_payload_json = payload_json
                yield f"data: {payload_json}\n\n"

            if job.status in ("done", "error"):
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/answer/{job_id}")
async def answer(job_id: str, answers: str = Form("")):
    """Submit answers to validation questions, unblock the pipeline thread."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "questions":
        return JSONResponse({"error": "No pending questions"}, status_code=400)

    with run_id_context(job_id):
        logger.info(
            "Job %s received clarification answers for %d question(s)",
            job_id,
            len(job.questions or []),
        )
        job.answers = answers
        web_jobs.append_clarification_round_for_ui(
            job, answers, parse_validation_answers_fn=parse_validation_answers
        )
        job.status = "running"
        _notify_job(job)
        job.answers_event.set()
    return JSONResponse({"status": "ok"})


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

    source_id_value = source_id.strip()
    if (
        not source_id_value
        or job.optional_pdf_allowed_ids is None
        or source_id_value not in job.optional_pdf_allowed_ids
    ):
        return JSONResponse({"error": "Invalid source_id"}, status_code=400)

    pdf_url_value = pdf_url.strip()
    raw: bytes | None = None
    if pdf_url_value:
        raw, error = web_jobs.fetch_pdf_bytes_from_url(pdf_url_value)
        if error:
            return JSONResponse({"error": error}, status_code=400)
    elif file is not None and file.filename:
        if not file.filename.lower().endswith(".pdf"):
            return JSONResponse(
                {"error": "Only PDF files are accepted"},
                status_code=400,
            )
        raw = await file.read()
    else:
        return JSONResponse(
            {"error": "Provide a PDF file or a PDF URL"},
            status_code=400,
        )

    with run_id_context(job_id):
        error = web_jobs.apply_optional_pdf_bytes(job, job_id, source_id_value, raw)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        job.optional_pdf_choices[source_id_value] = "url" if pdf_url_value else "file"
        logger.info(
            "Job %s attached optional PDF for source %s via %s",
            job_id,
            source_id_value,
            "url" if pdf_url_value else "file",
        )
    return JSONResponse({"status": "ok", "source_id": source_id_value})


@app.post("/optional-pdf/{job_id}/done")
async def optional_pdf_done(job_id: str):
    """Continue the pipeline after optional PDF uploads (or skip)."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "optional_pdfs":
        return JSONResponse({"error": "No optional PDF step active"}, status_code=400)

    with run_id_context(job_id):
        logger.info("Job %s completed optional PDF input step", job_id)
        web_jobs.append_optional_pdf_round_for_ui(job)
        job.status = "running"
        _notify_job(job)
        job.optional_pdf_event.set()
    return JSONResponse({"status": "ok"})


@app.post("/source-shortfall/{job_id}")
async def source_shortfall_decision(
    job_id: str,
    decision: str = Form(""),
    added_ids: str = Form(""),
):
    """Submit proceed/cancel decision after source recovery still falls short.

    *added_ids* is an optional JSON array of source IDs the user picked from
    the borderline candidates list.
    """
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "source_shortfall":
        return JSONResponse(
            {"error": "No source shortfall step active"}, status_code=400
        )

    choice = decision.strip().lower()
    if choice not in {"proceed", "cancel"}:
        return JSONResponse({"error": "Invalid decision"}, status_code=400)

    # Parse optional added_ids (JSON array of source ID strings).
    parsed_ids: list[str] = []
    raw_ids = added_ids.strip()
    if raw_ids and choice == "proceed":
        try:
            parsed_ids = json.loads(raw_ids)
            if not isinstance(parsed_ids, list):
                parsed_ids = []
            parsed_ids = [str(sid).strip() for sid in parsed_ids if str(sid).strip()]
        except (json.JSONDecodeError, TypeError):
            parsed_ids = []

    with run_id_context(job_id):
        logger.info(
            "Job %s source shortfall decision: %s (added_ids=%d)",
            job_id,
            choice,
            len(parsed_ids),
        )
        job.source_shortfall_decision = choice
        job.source_shortfall_added_ids = parsed_ids
        job.status = "running"
        _notify_job(job)
        job.source_shortfall_event.set()
    return JSONResponse(
        {"status": "ok", "decision": choice, "added_count": len(parsed_ids)}
    )


@app.get("/download/{job_id}")
async def download(job_id: str, format: str | None = Query(None)):
    """Return the result zip or just the docx."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "done":
        return JSONResponse({"error": "Job not ready"}, status_code=400)

    with run_id_context(job_id):
        logger.info("Job %s download requested (format=%s)", job_id, format or "zip")

    if format == "docx":
        docx_path = job.run_dir / "essay.docx"
        if not docx_path.is_file():
            return JSONResponse({"error": "Docx file not found"}, status_code=404)

        def _iter_docx():
            with open(docx_path, "rb") as fh:
                while chunk := fh.read(64 * 1024):
                    yield chunk

        return StreamingResponse(
            _iter_docx(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename=essay_{job_id}.docx"
            },
        )

    buffer = web_jobs.build_zip(job.run_dir)

    def _iter_zip():
        try:
            while chunk := buffer.read(64 * 1024):
                yield chunk
        finally:
            buffer.close()

    return StreamingResponse(
        _iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=essay_{job_id}.zip"},
    )


@app.post("/download/{job_id}/cleanup")
async def cleanup_download(job_id: str):
    """Delete a completed job after the client has received the ZIP."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status != "done":
        return JSONResponse({"error": "Job not ready"}, status_code=400)
    with run_id_context(job_id):
        logger.info("Job %s cleanup requested", job_id)
        web_jobs.delete_job_artifacts(job_id)
    return JSONResponse({"status": "ok"})
