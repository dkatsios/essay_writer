"""Background worker process for queued essay jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from config.settings import ModelsConfig, load_config
from src.agent import create_async_client
from src.intake import build_extracted_text, scan
from src.pipeline import run_pipeline
from src.runtime import parse_validation_answers
from src.tools._http import close_http_clients
from src import web_jobs

logger = logging.getLogger(__name__)


async def _heartbeat(job_id: str, *, worker_id: str) -> None:
    config = load_config()
    while True:
        await asyncio.sleep(config.worker_heartbeat_interval_seconds)
        if not web_jobs.jobs.renew_lease(
            job_id,
            worker_id=worker_id,
            lease_seconds=config.worker_lease_seconds,
        ):
            logger.warning("Lease renewal failed for job %s", job_id)
            return


async def run_claimed_job(job: web_jobs.Job, *, worker_id: str) -> None:
    has_uploads, has_user_sources = web_jobs.infer_job_has_uploads(job)
    if job.status == "pending":
        job.status = "running"
        web_jobs.save_job(job)
        web_jobs.notify_job(job)

    heartbeat_task = asyncio.create_task(_heartbeat(job.job_id, worker_id=worker_id))
    try:
        await web_jobs.run_pipeline_task(
            job,
            has_uploads,
            web_jobs.build_job_extra_prompt(job),
            job.min_sources,
            has_user_sources,
            load_config_fn=load_config,
            models_config_cls=ModelsConfig,
            create_async_client_fn=create_async_client,
            run_pipeline_fn=run_pipeline,
            scan_fn=scan,
            build_extracted_text_fn=build_extracted_text,
            parse_validation_answers_fn=parse_validation_answers,
            is_academic_level_question_fn=web_jobs.is_academic_level_question,
            interaction_timeout_seconds_fn=web_jobs.interaction_timeout_seconds,
        )
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        web_jobs.jobs.release_claim(job.job_id, worker_id=worker_id)


async def run_worker_once(*, worker_id: str | None = None) -> bool:
    config = load_config()
    resolved_worker_id = worker_id or web_jobs.worker_identity()
    job = web_jobs.jobs.claim_next_job(
        worker_id=resolved_worker_id,
        lease_seconds=config.worker_lease_seconds,
    )
    if job is None:
        return False
    await run_claimed_job(job, worker_id=resolved_worker_id)
    return True


async def worker_loop(*, worker_id: str | None = None) -> None:
    resolved_worker_id = worker_id or web_jobs.worker_identity()
    config = load_config()
    logger.info("Worker started as %s", resolved_worker_id)
    try:
        while True:
            claimed = await run_worker_once(worker_id=resolved_worker_id)
            if not claimed:
                await asyncio.sleep(config.worker_poll_interval_seconds)
    finally:
        close_http_clients()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
