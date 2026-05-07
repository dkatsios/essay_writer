from __future__ import annotations

import asyncio
import time

import pytest


def test_claim_next_job_claims_oldest_pending_job():
    from src.web_jobs import Job, jobs

    jobs.save(
        Job(
            job_id="jobold000001",
            status="pending",
            run_dir="runs/one",
            created_at=1.0,
        )
    )
    jobs.save(
        Job(
            job_id="jobnew000001",
            status="pending",
            run_dir="runs/two",
            created_at=2.0,
        )
    )

    claimed = jobs.claim_next_job(
        worker_id="worker-a",
        lease_seconds=60,
        current_time=10.0,
    )

    assert claimed is not None
    assert claimed.job_id == "jobold000001"

    refreshed = jobs.refresh("jobold000001")
    assert refreshed is not None
    assert refreshed.worker_id == "worker-a"
    assert refreshed.leased_at == 10.0
    assert refreshed.lease_expires_at == 70.0


def test_save_job_preserves_newer_lease_for_same_worker():
    from src.web_jobs import Job, jobs, save_job

    job = Job(
        job_id="leasekeep001",
        status="running",
        run_dir="runs/lease",
        worker_id="worker-a",
        leased_at=10.0,
        lease_expires_at=70.0,
    )
    save_job(job)

    assert jobs.renew_lease(
        job.job_id,
        worker_id="worker-a",
        lease_seconds=60,
        current_time=40.0,
    )

    job.current_step = "research"
    save_job(job)

    refreshed = jobs.refresh(job.job_id)
    assert refreshed is not None
    assert refreshed.worker_id == "worker-a"
    assert refreshed.leased_at == 40.0
    assert refreshed.lease_expires_at == 100.0


async def test_run_worker_once_claims_and_releases_job(monkeypatch):
    from src.web_jobs import Job, jobs, save_job
    from src.worker import run_worker_once

    job = Job(job_id="workerjob001", status="pending", run_dir="runs/test")
    save_job(job)

    async def fake_run_pipeline_task(job, *args, **kwargs):
        assert job.status == "running"
        assert job.worker_id == "worker-a"
        job.status = "done"
        save_job(job)

    monkeypatch.setattr("src.worker.web_jobs.run_pipeline_task", fake_run_pipeline_task)
    monkeypatch.setattr(
        "src.worker.web_jobs.infer_job_has_uploads", lambda j: (False, False)
    )

    claimed = await run_worker_once(worker_id="worker-a")

    assert claimed is True
    refreshed = jobs.refresh("workerjob001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.worker_id == ""
    assert refreshed.leased_at is None
    assert refreshed.lease_expires_at is None


async def test_run_worker_once_purges_job_after_delete_request(monkeypatch):
    from src.run_history_store import run_history
    from src.web_jobs import Job, jobs, save_job
    from src.worker import run_worker_once

    job = Job(job_id="workerpurge01", status="pending", run_dir="runs/workerpurge01")
    save_job(job)

    async def fake_run_pipeline_task(job, *args, **kwargs):
        job.status = "cancelled"
        job.delete_requested = True
        save_job(job)

    monkeypatch.setattr("src.worker.web_jobs.run_pipeline_task", fake_run_pipeline_task)
    monkeypatch.setattr(
        "src.worker.web_jobs.infer_job_has_uploads", lambda j: (False, False)
    )

    claimed = await run_worker_once(worker_id="worker-a")

    assert claimed is True
    assert jobs.refresh("workerpurge01") is None
    assert run_history.get_runtime_summary("workerpurge01") is None


@pytest.mark.asyncio
async def test_worker_loop_retries_after_operational_error(monkeypatch):
    from config.settings import load_config
    from sqlalchemy.exc import OperationalError

    from src import worker

    disposed: list[str] = []
    sleep_calls: list[int] = []
    attempts = 0

    async def fake_run_worker_once(*, worker_id: str | None = None) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("SELECT 1", {}, Exception("timeout"))
        raise asyncio.CancelledError()

    async def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(worker, "run_worker_once", fake_run_worker_once)
    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        worker.web_jobs.jobs, "dispose_engine", lambda: disposed.append("jobs")
    )
    monkeypatch.setattr(
        worker.run_history, "dispose_engine", lambda: disposed.append("history")
    )
    monkeypatch.setattr(worker, "close_http_clients", lambda: None)
    monkeypatch.setattr(worker.web_jobs, "worker_identity", lambda: "worker-a")

    with pytest.raises(asyncio.CancelledError):
        await worker.worker_loop(worker_id="worker-a")

    assert attempts == 1
    assert sleep_calls == [load_config().worker_poll_interval_seconds]
    assert disposed == ["jobs", "history"]


def test_requeue_for_retry_requeues_below_max():
    from src.web_jobs import Job, requeue_for_retry, jobs

    job = Job(
        job_id="retryjob0001", status="error", run_dir="runs/retry", retry_count=0
    )
    jobs.save(job)

    result = requeue_for_retry(job)

    assert result is True
    assert job.status == "pending"
    assert job.retry_count == 1
    assert job.not_before is not None
    assert job.not_before > time.time()
    assert job.error == ""


def test_requeue_for_retry_refuses_at_max():
    from src.web_jobs import Job, _MAX_JOB_RETRIES, requeue_for_retry, jobs

    job = Job(
        job_id="retryjob0002",
        status="error",
        run_dir="runs/retry",
        retry_count=_MAX_JOB_RETRIES,
    )
    jobs.save(job)

    result = requeue_for_retry(job)

    assert result is False
    assert job.status == "error"


def test_claim_next_job_skips_job_with_future_not_before():
    from src.web_jobs import Job, jobs

    jobs.save(
        Job(
            job_id="notbefore001",
            status="pending",
            run_dir="runs/nb",
            created_at=1.0,
            not_before=100.0,
        )
    )

    # At time=50, the job should not be claimable
    claimed = jobs.claim_next_job(
        worker_id="worker-b",
        lease_seconds=60,
        current_time=50.0,
    )
    assert claimed is None

    # At time=100, the job should be claimable
    claimed = jobs.claim_next_job(
        worker_id="worker-b",
        lease_seconds=60,
        current_time=100.0,
    )
    assert claimed is not None
    assert claimed.job_id == "notbefore001"


def test_is_retryable_error_detects_rate_limit():
    from src.web_jobs import is_retryable_error

    assert is_retryable_error(Exception("429 RESOURCE_EXHAUSTED")) is True
    assert is_retryable_error(TimeoutError("connect timeout")) is True
    assert is_retryable_error(ValueError("bad input")) is False
