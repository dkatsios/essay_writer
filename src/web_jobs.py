"""Job state and background execution helpers for the web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import urlparse

from config.settings import load_config
from src.job_store import JobStore
from src.run_history_store import run_history
from src.runtime import TokenTracker
from src.schemas import AssignmentBrief, Clarification, ValidationQuestion
from src.pipeline_sources import SourceShortfallAbort
from src.run_logging import (
    run_id_context,
    setup_run_logging,
    teardown_run_logging,
)
from src.storage import AnyStorage, create_run_storage
from src.tools._http import pdf_get
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
    worker_id: str = ""
    leased_at: float | None = None
    lease_expires_at: float | None = None
    run_dir: str = ""  # R2 prefix key for this run's storage
    questions: list[dict] | None = None
    answers_event: asyncio.Event = field(default_factory=asyncio.Event)
    answers: str = ""
    optional_pdf_items: list[dict] | None = None
    optional_pdf_allowed_ids: frozenset[str] | None = None
    optional_pdf_event: asyncio.Event = field(default_factory=asyncio.Event)
    source_shortfall: dict | None = None
    source_shortfall_event: asyncio.Event = field(default_factory=asyncio.Event)
    source_shortfall_decision: str = ""
    source_shortfall_added_ids: list[str] = field(default_factory=list)
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
    current_step: str = ""
    step_index: int | None = None
    step_count: int | None = None
    api_key: str = ""
    _sse_event: asyncio.Event = field(default_factory=asyncio.Event)

    def get_storage(self) -> AnyStorage:
        """Create storage for this job's run prefix."""
        return create_run_storage(self.job_id)


jobs = JobStore()


def _sync_job_artifacts(job: Job) -> None:
    run_history.sync_artifacts(job.job_id, job.get_storage())


def _persist_run_history_snapshot(job: Job) -> None:
    tracker = job.tracker
    if tracker is not None and hasattr(tracker, "build_runtime_summary"):
        payload = tracker.build_runtime_summary(
            job.get_storage(),
            status=job.status,
            provider=job.provider,
        )
    else:
        payload = {
            "status": job.status,
            "provider": job.provider,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_thinking_tokens": 0,
            "total_duration_seconds": 0.0,
            "step_count": 0,
            "target_words": job.target_words,
            "draft_words": 0,
            "final_words": 0,
        }
    if job.target_words is not None and not payload.get("target_words"):
        payload["target_words"] = job.target_words
    run_history.save_runtime_summary(job.job_id, **payload)


def _persist_terminal_run_history(job: Job, *, status: str) -> None:
    tracker = job.tracker
    if tracker is not None and hasattr(tracker, "build_runtime_summary"):
        payload = tracker.build_runtime_summary(
            job.get_storage(),
            status=status,
            provider=job.provider,
        )
    else:
        payload = {
            "status": status,
            "provider": job.provider,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_thinking_tokens": 0,
            "total_duration_seconds": 0.0,
            "step_count": 0,
            "target_words": job.target_words,
            "draft_words": 0,
            "final_words": 0,
        }
    run_history.save_runtime_summary(job.job_id, **payload)
    _sync_job_artifacts(job)


def save_job(job: Job) -> Job:
    """Persist the current durable view of *job* and remember local transients."""
    saved = jobs.save(job)
    _persist_run_history_snapshot(saved)
    return saved


def notify_job(job: Job) -> None:
    job._sse_event.set()


def build_status_payload(job: Job) -> dict:
    payload: dict = {"status": job.status}
    if job.status == "running":
        stage = job.current_step
        if not stage and job.tracker is not None:
            stage = job.tracker.get_current_step()
        if stage:
            payload["stage"] = stage

        if job.step_index is not None:
            payload["step_index"] = job.step_index
        elif job.tracker is not None:
            with job.tracker._lock:
                payload["step_index"] = job.tracker.step_index

        if job.step_count is not None:
            payload["step_count"] = job.step_count
        elif job.tracker is not None:
            with job.tracker._lock:
                payload["step_count"] = job.tracker.step_count

        if job.tracker is not None:
            with job.tracker._lock:
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
    return load_config().web_interaction_timeout_seconds


def _job_ttl_seconds() -> int:
    return load_config().web_job_ttl_seconds


def _job_sweep_interval_seconds() -> int:
    return load_config().web_job_sweep_interval_seconds


def set_job_error(job: Job, message: str) -> None:
    logger.error("Job %s failed: %s", job.job_id, message)
    job.questions = None
    job.optional_pdf_items = None
    job.optional_pdf_allowed_ids = None
    job.source_shortfall = None
    job.source_shortfall_decision = ""
    job.status = "error"
    job.error = message
    job.finished_at = time.time()
    save_job(job)
    notify_job(job)


class JobInteractionTimeout(Exception):
    """Raised when a web job waits too long for user interaction."""


def _copy_job_state(target: Job, latest: Job) -> None:
    target.status = latest.status
    target.worker_id = latest.worker_id
    target.leased_at = latest.leased_at
    target.lease_expires_at = latest.lease_expires_at
    target.questions = latest.questions
    target.answers = latest.answers
    target.optional_pdf_items = latest.optional_pdf_items
    target.optional_pdf_allowed_ids = latest.optional_pdf_allowed_ids
    target.source_shortfall = latest.source_shortfall
    target.source_shortfall_decision = latest.source_shortfall_decision
    target.source_shortfall_added_ids = list(latest.source_shortfall_added_ids)
    target.error = latest.error
    target.finished_at = latest.finished_at
    target.clarification_rounds = list(latest.clarification_rounds)
    target.optional_pdf_rounds = list(latest.optional_pdf_rounds)
    target.optional_pdf_choices = dict(latest.optional_pdf_choices)
    target.current_step = latest.current_step
    target.step_index = latest.step_index
    target.step_count = latest.step_count


async def async_wait_for_job_signal(
    job: Job,
    event: asyncio.Event,
    *,
    error_message: str,
    timeout: int | None = None,
    interaction_timeout_seconds_fn=interaction_timeout_seconds,
) -> bool:
    wait_seconds = interaction_timeout_seconds_fn() if timeout is None else timeout
    expected_status = job.status
    deadline = time.monotonic() + wait_seconds
    while True:
        if event.is_set():
            return True

        latest = jobs.refresh(job.job_id)
        if latest is not None:
            _copy_job_state(job, latest)
            if latest.status != expected_status:
                return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            set_job_error(job, error_message)
            return False

        try:
            await asyncio.wait_for(event.wait(), timeout=min(1.0, remaining))
        except TimeoutError:
            pass


def delete_job_artifacts(job_id: str) -> bool:
    job = jobs.pop(job_id, None)
    if job is None:
        return False
    run_history.mark_artifacts_deleted(job_id)
    try:
        storage = job.get_storage()
        storage.delete_all()
    except Exception:
        logger.warning("Failed to delete R2 artifacts for job %s", job_id)
    return True


def job_ttl_sweep_once(now: float | None = None) -> int:
    ttl = _job_ttl_seconds()
    if ttl <= 0:
        return 0
    current_time = time.time() if now is None else now
    removed = 0
    for job in jobs.expired_finished_jobs(current_time=current_time, ttl_seconds=ttl):
        jobs.pop(job.job_id, None)
        run_history.mark_artifacts_deleted(job.job_id, current_time=current_time)
        try:
            storage = job.get_storage()
            storage.delete_all()
        except Exception:
            logger.warning(
                "Failed to delete R2 artifacts for job %s during TTL sweep", job.job_id
            )
        removed += 1
        logger.info(
            "TTL cleanup removed job %s (status=%s, age=%.0fs)",
            job.job_id,
            job.status,
            current_time - job.finished_at,
        )
    return removed


async def _job_ttl_sweeper_loop() -> None:
    interval = _job_sweep_interval_seconds()
    while True:
        await asyncio.sleep(interval)
        try:
            job_ttl_sweep_once()
        except Exception:
            logger.exception("Job TTL sweep failed")


def start_job_ttl_sweeper() -> None:
    if _job_ttl_seconds() <= 0:
        logger.info("ESSAY_WEB_JOB_TTL_SECONDS is 0; stale-job sweeper disabled")
        return
    asyncio.create_task(_job_ttl_sweeper_loop())


def mark_stale_jobs_on_startup() -> int:
    """Fail previously active jobs so the UI sees a deterministic terminal state."""
    config = load_config()
    if not config.database.mark_stale_jobs_on_startup:
        return 0
    count = jobs.mark_stale_active_jobs(
        "Server restarted while this job was active. Please submit it again."
    )
    if count:
        logger.warning("Marked %d stale active job(s) as failed on startup", count)
    return count


def build_job_extra_prompt(job: Job) -> str | None:
    extra_prompt = job.submit_prompt.strip() or None
    if job.target_words is not None and job.target_words > 0:
        words_line = f"Target word count: {job.target_words} words."
        extra_prompt = f"{words_line}\n{extra_prompt}" if extra_prompt else words_line
    if job.academic_level:
        level_line = f"Academic level: {job.academic_level}."
        extra_prompt = f"{level_line}\n{extra_prompt}" if extra_prompt else level_line
    return extra_prompt


def infer_job_has_uploads(job: Job) -> tuple[bool, bool]:
    """Check if uploads and user_sources exist in storage.

    Returns (has_uploads, has_user_sources).
    """
    storage = job.get_storage()
    has_uploads = bool(storage.list_files("uploads/"))
    has_user_sources = bool(storage.list_files("user_sources/"))
    return has_uploads, has_user_sources


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def run_pipeline_task(
    job: Job,
    has_uploads: bool,
    prompt: str | None,
    min_sources: int | None = None,
    has_user_sources: bool = False,
    *,
    load_config_fn,
    models_config_cls,
    create_async_client_fn,
    run_pipeline_fn,
    scan_fn,
    build_extracted_text_fn,
    parse_validation_answers_fn,
    is_academic_level_question_fn=is_academic_level_question,
    interaction_timeout_seconds_fn=interaction_timeout_seconds,
) -> None:
    """Execute the essay pipeline as an asyncio task on uvicorn's event loop."""
    log_handler = None
    storage = job.get_storage()
    with run_id_context(job.job_id):
        try:
            log_handler = setup_run_logging(None, job.job_id)
            logger.info(
                "Job %s started (provider=%s, target_words=%s, min_sources=%s)",
                job.job_id,
                job.provider or "default",
                job.target_words,
                min_sources,
            )

            config = load_config_fn()

            if job.provider:
                config.models = models_config_cls(provider=job.provider)

            if has_uploads:
                # Download uploaded files to temp, scan, extract text, write to storage
                import tempfile

                with tempfile.TemporaryDirectory() as tmpdir:
                    from pathlib import Path

                    tmp_path = Path(tmpdir)
                    for subpath in storage.list_files("uploads/"):
                        filename = subpath.rsplit("/", 1)[-1]
                        (tmp_path / filename).write_bytes(storage.read_bytes(subpath))
                    input_files = scan_fn(str(tmp_path))
                    extracted_text = build_extracted_text_fn(
                        input_files, extra_prompt=prompt
                    )
                    del input_files
            elif prompt:
                extracted_text = f"# Assignment\n\n{prompt}\n"
            else:
                job.status = "error"
                job.error = "Provide at least a prompt or upload files."
                job.finished_at = time.time()
                save_job(job)
                _persist_terminal_run_history(job, status="error")
                notify_job(job)
                return

            storage.write_text("input/extracted.md", extracted_text)
            _sync_job_artifacts(job)

            api_key = job.api_key or None
            job.api_key = ""
            async_worker = create_async_client_fn(config.models.worker, api_key=api_key)
            async_writer = create_async_client_fn(config.models.writer, api_key=api_key)
            async_reviewer = create_async_client_fn(
                config.models.reviewer, api_key=api_key
            )

            tracker = TokenTracker()
            job.tracker = tracker

            def _on_progress() -> None:
                job.current_step = tracker.get_current_step() or ""
                with tracker._lock:
                    job.step_index = tracker.step_index
                    job.step_count = tracker.step_count
                save_job(job)
                notify_job(job)

            tracker.set_on_progress(_on_progress)
            save_job(job)

            async def _on_questions(
                questions: list[ValidationQuestion], run_storage: AnyStorage
            ) -> None:
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
                    brief = AssignmentBrief.model_validate_json(
                        run_storage.read_text("brief/assignment.json")
                    )
                    if brief.clarifications is None:
                        brief.clarifications = []
                    brief.clarifications.extend(auto_clarifications)
                    if not brief.academic_level:
                        brief.academic_level = job.academic_level
                    run_storage.write_text(
                        "brief/assignment.json",
                        brief.model_dump_json(indent=2, ensure_ascii=False),
                    )

                if not remaining:
                    return

                chosen_answers = ""
                if not config.writing.interactive_validation:
                    chosen_answers = ", ".join(
                        f"{index}. {chr(ord('a') + max(0, min(question.suggested_option_index, len(question.options) - 1)))}"
                        for index, question in enumerate(remaining, 1)
                        if question.options
                    )
                    logger.info(
                        "Job %s auto-applied %d suggested clarification answer(s)",
                        job.job_id,
                        len(remaining),
                    )

                else:
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
                    save_job(job)
                    logger.info(
                        "Job %s waiting for %d clarification question(s)",
                        job.job_id,
                        len(remaining),
                    )
                    notify_job(job)
                    if not await async_wait_for_job_signal(
                        job,
                        job.answers_event,
                        error_message="Timed out waiting for clarification answers.",
                        interaction_timeout_seconds_fn=interaction_timeout_seconds_fn,
                    ):
                        raise JobInteractionTimeout()

                    chosen_answers = job.answers
                    if not chosen_answers:
                        logger.info("Job %s clarification step skipped", job.job_id)
                        job.questions = None
                        save_job(job)
                        return

                brief = AssignmentBrief.model_validate_json(
                    run_storage.read_text("brief/assignment.json")
                )
                if brief.clarifications is None:
                    brief.clarifications = []
                brief.clarifications.extend(
                    parse_validation_answers_fn(remaining, chosen_answers)
                )
                run_storage.write_text(
                    "brief/assignment.json",
                    brief.model_dump_json(indent=2, ensure_ascii=False),
                )
                logger.info("Job %s clarification answers saved", job.job_id)
                job.questions = None
                save_job(job)

            async def _on_optional_pdfs(
                run_storage: AnyStorage, items: list[dict]
            ) -> None:
                if not items:
                    return
                if job.fast_track:
                    logger.info(
                        "Job %s skipped optional PDF prompt due to fast_track",
                        job.job_id,
                    )
                    return
                job.optional_pdf_choices.clear()
                job.optional_pdf_items = items
                job.optional_pdf_allowed_ids = frozenset(
                    str(item["source_id"]) for item in items
                )
                job.optional_pdf_event.clear()
                job.status = "optional_pdfs"
                _sync_job_artifacts(job)
                save_job(job)
                logger.info(
                    "Job %s waiting for optional PDFs for %d source(s)",
                    job.job_id,
                    len(items),
                )
                notify_job(job)
                if not await async_wait_for_job_signal(
                    job,
                    job.optional_pdf_event,
                    error_message="Timed out waiting for optional PDF input.",
                    interaction_timeout_seconds_fn=interaction_timeout_seconds_fn,
                ):
                    raise JobInteractionTimeout()
                job.optional_pdf_items = None
                job.optional_pdf_allowed_ids = None
                save_job(job)
                logger.info("Job %s optional PDF step resumed", job.job_id)

            async def _on_source_shortfall(
                run_storage: AnyStorage, summary: dict
            ) -> tuple[bool, list[str]]:
                job.source_shortfall = summary
                job.source_shortfall_decision = ""
                job.source_shortfall_added_ids = []
                job.source_shortfall_event.clear()
                job.status = "source_shortfall"
                _sync_job_artifacts(job)
                save_job(job)
                logger.warning(
                    "Job %s waiting for source shortfall decision (%s/%s usable sources)",
                    job.job_id,
                    summary.get("usable_sources"),
                    summary.get("target_sources"),
                )
                notify_job(job)
                if not await async_wait_for_job_signal(
                    job,
                    job.source_shortfall_event,
                    error_message="Timed out waiting for source shortfall decision.",
                    interaction_timeout_seconds_fn=interaction_timeout_seconds_fn,
                ):
                    raise JobInteractionTimeout()
                decision = job.source_shortfall_decision.strip().lower()
                added_ids = list(job.source_shortfall_added_ids)
                job.source_shortfall = None
                job.source_shortfall_added_ids = []
                save_job(job)
                return decision == "proceed", added_ids

            await run_pipeline_fn(
                None,
                None,
                None,
                storage,
                config,
                extra_prompt=prompt,
                token_tracker=tracker,
                on_questions=_on_questions,
                on_optional_source_pdfs=_on_optional_pdfs,
                on_source_shortfall=_on_source_shortfall,
                min_sources=min_sources,
                user_sources_dir="user_sources" if has_user_sources else None,
                async_worker=async_worker,
                async_writer=async_writer,
                async_reviewer=async_reviewer,
                job_id=job.job_id,
                run_history_store=run_history,
            )

            tracker.write_report(storage)
            _persist_terminal_run_history(job, status="done")
            job.status = "done"
            job.current_step = ""
            job.step_index = None
            job.step_count = None
            job.finished_at = time.time()
            save_job(job)
            logger.info("Job %s completed successfully", job.job_id)
            notify_job(job)

        except JobInteractionTimeout:
            _persist_terminal_run_history(job, status="error")
            return
        except SourceShortfallAbort as exc:
            logger.warning("Job %s aborted after source shortfall: %s", job.job_id, exc)
            set_job_error(job, str(exc))
            _persist_terminal_run_history(job, status="error")
        except Exception:
            logger.exception("Pipeline failed for job %s", job.job_id)
            set_job_error(job, "Pipeline failed. Check server logs for details.")
            _persist_terminal_run_history(job, status="error")
        finally:
            if log_handler is not None:
                teardown_run_logging(log_handler)


def build_zip(storage: AnyStorage) -> BytesIO:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for subpath in sorted(storage.iter_all_files()):
            zip_file.writestr(subpath, storage.read_bytes(subpath))
    buffer.seek(0)
    return buffer


def fetch_pdf_bytes_from_url(url: str) -> tuple[bytes | None, str | None]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "Invalid URL (use http or https)"
    try:
        resp = pdf_get(url, max_retries=2, initial_backoff=1.0)
    except Exception as exc:
        logger.warning("Optional PDF URL fetch failed: %s", exc)
        return None, "Could not download URL"
    raw = resp.content
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

    storage = job.get_storage()
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in source_id)[:120]
    content_subpath = f"sources/supplement/{safe_name}.txt"
    storage.write_text(content_subpath, text)

    registry = json.loads(storage.read_text("sources/registry.json"))
    if source_id not in registry:
        return "Source not in registry"
    registry[source_id]["content_path"] = content_subpath
    storage.write_text(
        "sources/registry.json",
        json.dumps(registry, ensure_ascii=False, indent=2),
    )
    run_history.sync_artifacts(job.job_id, storage)
    return None
