"""Job state and background execution helpers for the web UI."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.runtime import TokenTracker
from src.schemas import AssignmentBrief, Clarification, ValidationQuestion
from src.pipeline_sources import SourceShortfallAbort
from src.tools._http import http_get
from src.tools.web_fetcher import extract_pdf_bytes_to_text

logger = logging.getLogger(__name__)

_DEFAULT_JOB_TTL_SECONDS = 86_400
_DEFAULT_SWEEP_INTERVAL_SECONDS = 300
_DEFAULT_INTERACTION_TIMEOUT_SECONDS = 1_800
_MAX_OPTIONAL_PDF_BYTES = 30 * 1024 * 1024

_LEVEL_KEYWORDS = {
    "ακαδημαϊκό επίπεδο",
    "ακαδημαικό επίπεδο",
    "academic level",
    "προπτυχιακό",
    "μεταπτυχιακό",
    "επίπεδο σπουδών",
    "επιπεδο σπουδων",
}
_LEVEL_KEYWORDS_NFC = {unicodedata.normalize("NFC", kw) for kw in _LEVEL_KEYWORDS}


def is_academic_level_question(question: ValidationQuestion) -> bool:
    text = unicodedata.normalize("NFC", question.question.lower())
    return any(keyword in text for keyword in _LEVEL_KEYWORDS_NFC)


@dataclass
class Job:
    """In-memory state for a single pipeline run."""

    job_id: str
    status: str = "running"
    run_dir: Path = field(default_factory=lambda: Path("."))
    questions: list[dict] | None = None
    answers_event: threading.Event = field(default_factory=threading.Event)
    answers: str = ""
    optional_pdf_items: list[dict] | None = None
    optional_pdf_allowed_ids: frozenset[str] | None = None
    optional_pdf_event: threading.Event = field(default_factory=threading.Event)
    source_shortfall: dict | None = None
    source_shortfall_event: threading.Event = field(default_factory=threading.Event)
    source_shortfall_decision: str = ""
    error: str = ""
    academic_level: str = ""
    submit_prompt: str = ""
    target_words: int | None = None
    min_sources: int | None = None
    tracker: TokenTracker | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    clarification_rounds: list[dict] = field(default_factory=list)
    optional_pdf_rounds: list[dict] = field(default_factory=list)
    optional_pdf_choices: dict[str, str] = field(default_factory=dict)
    fast_track: bool = False
    provider: str = ""
    api_key: str = ""
    _sse_event: threading.Event = field(default_factory=threading.Event)


jobs: dict[str, Job] = {}


def notify_job(job: Job) -> None:
    job._sse_event.set()


def build_status_payload(job: Job) -> dict:
    payload: dict = {"status": job.status}
    if job.status == "running" and job.tracker is not None:
        payload["stage"] = job.tracker.get_current_step()
        with job.tracker._lock:
            payload["step_index"] = job.tracker.step_index
            payload["step_count"] = job.tracker.step_count
            if job.tracker.sub_total > 0:
                payload["sub_done"] = job.tracker.sub_done
                payload["sub_total"] = job.tracker.sub_total
    if job.status == "questions" and job.questions:
        payload["questions"] = job.questions
    if job.status == "optional_pdfs" and job.optional_pdf_items:
        payload["optional_pdf_items"] = job.optional_pdf_items
    if job.status == "source_shortfall" and job.source_shortfall:
        payload["source_shortfall"] = job.source_shortfall
    if job.status == "error":
        payload["error"] = job.error
    payload["clarification_rounds"] = job.clarification_rounds
    payload["optional_pdf_rounds"] = job.optional_pdf_rounds
    payload["submit"] = {
        "prompt": job.submit_prompt,
        "academic_level": job.academic_level,
        "target_words": job.target_words,
        "min_sources": job.min_sources,
        "provider": job.provider,
    }
    return payload


def append_clarification_round_for_ui(
    job: Job,
    answers: str,
    *,
    parse_validation_answers_fn,
) -> None:
    if not job.questions:
        return
    questions = [
        ValidationQuestion(
            question=item["question"],
            options=item["options"],
            suggested_option_index=int(item.get("suggested_option_index", 0)),
        )
        for item in job.questions
    ]
    parsed = parse_validation_answers_fn(questions, answers) if answers.strip() else []
    by_question = {item.question: item.answer for item in parsed}
    ui_items = [
        {"question": item["question"], "answer": by_question.get(item["question"], "—")}
        for item in job.questions
    ]
    job.clarification_rounds.append({"items": ui_items})


def append_optional_pdf_round_for_ui(job: Job) -> None:
    items = job.optional_pdf_items or []
    round_items: list[dict] = []
    for row in items:
        source_id = str(row["source_id"])
        how = job.optional_pdf_choices.get(source_id)
        if how == "file":
            answer = "PDF from file"
        elif how == "url":
            answer = "PDF from URL"
        else:
            answer = "— skipped / none"
        round_items.append(
            {"title": str(row.get("title") or source_id), "answer": answer}
        )
    job.optional_pdf_rounds.append({"items": round_items})
    job.optional_pdf_choices.clear()


def interaction_timeout_seconds() -> int:
    raw = os.environ.get(
        "ESSAY_WEB_INTERACTION_TIMEOUT_SECONDS",
        str(_DEFAULT_INTERACTION_TIMEOUT_SECONDS),
    ).strip()
    try:
        timeout = int(raw)
    except ValueError:
        return _DEFAULT_INTERACTION_TIMEOUT_SECONDS
    return max(1, timeout)


def _job_ttl_seconds() -> int:
    raw = os.environ.get(
        "ESSAY_WEB_JOB_TTL_SECONDS", str(_DEFAULT_JOB_TTL_SECONDS)
    ).strip()
    try:
        ttl = int(raw)
    except ValueError:
        return _DEFAULT_JOB_TTL_SECONDS
    return max(0, ttl)


def _job_sweep_interval_seconds() -> int:
    raw = os.environ.get(
        "ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS",
        str(_DEFAULT_SWEEP_INTERVAL_SECONDS),
    ).strip()
    try:
        interval = int(raw)
    except ValueError:
        return _DEFAULT_SWEEP_INTERVAL_SECONDS
    return max(60, interval)


def set_job_error(job: Job, message: str) -> None:
    job.questions = None
    job.optional_pdf_items = None
    job.optional_pdf_allowed_ids = None
    job.source_shortfall = None
    job.source_shortfall_decision = ""
    job.status = "error"
    job.error = message
    job.finished_at = time.time()
    notify_job(job)


class JobInteractionTimeout(Exception):
    """Raised when a web job waits too long for user interaction."""


def wait_for_job_signal(
    job: Job,
    event: threading.Event,
    *,
    error_message: str,
    timeout: int | None = None,
    interaction_timeout_seconds_fn=interaction_timeout_seconds,
) -> bool:
    wait_seconds = interaction_timeout_seconds_fn() if timeout is None else timeout
    if event.wait(wait_seconds):
        return True
    set_job_error(job, error_message)
    return False


def delete_job_artifacts(job_id: str) -> bool:
    job = jobs.pop(job_id, None)
    if job is None:
        return False
    shutil.rmtree(job.run_dir, ignore_errors=True)
    return True


def job_ttl_sweep_once(now: float | None = None) -> int:
    ttl = _job_ttl_seconds()
    if ttl <= 0:
        return 0
    current_time = time.time() if now is None else now
    removed = 0
    for job_id, job in list(jobs.items()):
        if job.status not in ("done", "error") or job.finished_at is None:
            continue
        if current_time - job.finished_at <= ttl:
            continue
        jobs.pop(job_id, None)
        shutil.rmtree(job.run_dir, ignore_errors=True)
        removed += 1
        logger.info(
            "TTL cleanup removed job %s (status=%s, age=%.0fs)",
            job_id,
            job.status,
            current_time - job.finished_at,
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


def start_job_ttl_sweeper() -> None:
    if _job_ttl_seconds() <= 0:
        logger.info("ESSAY_WEB_JOB_TTL_SECONDS is 0; stale-job sweeper disabled")
        return
    thread = threading.Thread(
        target=_job_ttl_sweeper_loop,
        name="essay-job-ttl-sweeper",
        daemon=True,
    )
    thread.start()


def run_pipeline_thread(
    job: Job,
    upload_dir: Path | None,
    prompt: str | None,
    min_sources: int | None = None,
    user_sources_dir: Path | None = None,
    *,
    load_config_fn,
    models_config_cls,
    create_client_fn,
    create_async_client_fn,
    run_pipeline_fn,
    scan_fn,
    build_extracted_text_fn,
    parse_validation_answers_fn,
    is_academic_level_question_fn=is_academic_level_question,
    interaction_timeout_seconds_fn=interaction_timeout_seconds,
) -> None:
    """Execute the essay pipeline in a background thread."""
    try:
        config = load_config_fn()

        if job.provider:
            config.models = models_config_cls(provider=job.provider)

        run_dir = job.run_dir
        input_dir = run_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        if upload_dir and any(upload_dir.iterdir()):
            input_files = scan_fn(str(upload_dir))
            extracted_text = build_extracted_text_fn(input_files, extra_prompt=prompt)
            del input_files
        elif prompt:
            extracted_text = f"# Assignment\n\n{prompt}\n"
        else:
            job.status = "error"
            job.error = "Provide at least a prompt or upload files."
            job.finished_at = time.time()
            notify_job(job)
            return

        (input_dir / "extracted.md").write_text(extracted_text, encoding="utf-8")

        api_key = job.api_key or None
        job.api_key = ""
        worker = create_client_fn(config.models.worker, api_key=api_key)
        async_worker = create_async_client_fn(config.models.worker, api_key=api_key)
        writer = create_client_fn(config.models.writer, api_key=api_key)
        reviewer = create_client_fn(config.models.reviewer, api_key=api_key)

        tracker = TokenTracker()
        job.tracker = tracker
        tracker.set_on_progress(lambda: notify_job(job))

        def _on_questions(questions: list[ValidationQuestion], run_path: Path) -> None:
            remaining = questions
            auto_clarifications: list[Clarification] = []
            if job.academic_level:
                remaining = []
                for question in questions:
                    if is_academic_level_question_fn(question):
                        auto_clarifications.append(
                            Clarification(
                                question=question.question,
                                answer=job.academic_level,
                            )
                        )
                    else:
                        remaining.append(question)

            if auto_clarifications:
                brief_path = run_path / "brief" / "assignment.json"
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

            if not remaining:
                return

            job.questions = [
                {
                    "question": question.question,
                    "options": question.options,
                    "suggested_option_index": question.suggested_option_index,
                }
                for question in remaining
            ]
            job.answers_event.clear()
            job.status = "questions"
            notify_job(job)
            if not wait_for_job_signal(
                job,
                job.answers_event,
                error_message="Timed out waiting for clarification answers.",
                interaction_timeout_seconds_fn=interaction_timeout_seconds_fn,
            ):
                raise JobInteractionTimeout()

            if not job.answers:
                job.questions = None
                return

            brief_path = run_path / "brief" / "assignment.json"
            brief = AssignmentBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8")
            )
            if brief.clarifications is None:
                brief.clarifications = []
            brief.clarifications.extend(
                parse_validation_answers_fn(remaining, job.answers)
            )
            brief_path.write_text(
                brief.model_dump_json(indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            job.questions = None

        def _on_optional_pdfs(run_path: Path, items: list[dict]) -> None:
            if not items or job.fast_track:
                return
            job.optional_pdf_choices.clear()
            job.optional_pdf_items = items
            job.optional_pdf_allowed_ids = frozenset(
                str(item["source_id"]) for item in items
            )
            job.optional_pdf_event.clear()
            job.status = "optional_pdfs"
            notify_job(job)
            if not wait_for_job_signal(
                job,
                job.optional_pdf_event,
                error_message="Timed out waiting for optional PDF input.",
                interaction_timeout_seconds_fn=interaction_timeout_seconds_fn,
            ):
                raise JobInteractionTimeout()
            job.optional_pdf_items = None
            job.optional_pdf_allowed_ids = None

        def _on_source_shortfall(run_path: Path, summary: dict) -> bool:
            job.source_shortfall = summary
            job.source_shortfall_decision = ""
            job.source_shortfall_event.clear()
            job.status = "source_shortfall"
            notify_job(job)
            if not wait_for_job_signal(
                job,
                job.source_shortfall_event,
                error_message="Timed out waiting for source shortfall decision.",
                interaction_timeout_seconds_fn=interaction_timeout_seconds_fn,
            ):
                raise JobInteractionTimeout()
            decision = job.source_shortfall_decision.strip().lower()
            job.source_shortfall = None
            return decision == "proceed"

        run_pipeline_fn(
            worker,
            writer,
            reviewer,
            run_dir,
            config,
            extra_prompt=prompt,
            token_tracker=tracker,
            on_questions=_on_questions
            if config.writing.interactive_validation
            else None,
            on_optional_source_pdfs=_on_optional_pdfs,
            on_source_shortfall=_on_source_shortfall,
            min_sources=min_sources,
            user_sources_dir=user_sources_dir,
            async_worker=async_worker,
        )

        tracker.write_report(run_dir)
        job.status = "done"
        job.finished_at = time.time()
        notify_job(job)

    except JobInteractionTimeout:
        return
    except SourceShortfallAbort as exc:
        set_job_error(job, str(exc))
    except Exception:
        logger.exception("Pipeline failed for job %s", job.job_id)
        set_job_error(job, "Pipeline failed. Check server logs for details.")


def build_zip(run_dir: Path) -> BytesIO:
    buffer = BytesIO()
    root = run_dir.resolve()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zip_file.write(path, path.relative_to(root).as_posix())
    buffer.seek(0)
    return buffer


def fetch_pdf_bytes_from_url(url: str) -> tuple[bytes | None, str | None]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "Invalid URL (use http or https)"
    try:
        response = http_get(
            url,
            follow_redirects=True,
            max_retries=2,
            initial_backoff=1.0,
            request_name="optional pdf url",
        )
    except httpx.HTTPError as exc:
        logger.warning("Optional PDF URL fetch failed: %s", exc)
        return None, "Could not download URL"
    raw = response.content
    if len(raw) > _MAX_OPTIONAL_PDF_BYTES:
        return None, "File too large"
    if not raw.startswith(b"%PDF"):
        return None, "URL did not return a PDF"
    return raw, None


def apply_optional_pdf_bytes(
    job: Job, job_id: str, source_id: str, raw: bytes
) -> str | None:
    if len(raw) > _MAX_OPTIONAL_PDF_BYTES:
        return "File too large"
    if not raw.startswith(b"%PDF"):
        return "Not a valid PDF"
    try:
        text = extract_pdf_bytes_to_text(raw)
    except Exception:
        logger.exception("PDF extract failed for job %s source %s", job_id, source_id)
        return "Could not read PDF text"
    if len(text) > 50_000:
        text = text[:50_000] + "\n\n[... truncated ...]"

    supplement_dir = job.run_dir / "sources" / "supplement"
    supplement_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in source_id)[:120]
    text_path = supplement_dir / f"{safe_name}.txt"
    text_path.write_text(text, encoding="utf-8")

    registry_path = job.run_dir / "sources" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if source_id not in registry:
        return "Source not in registry"
    registry[source_id]["content_path"] = str(text_path.resolve())
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return None
