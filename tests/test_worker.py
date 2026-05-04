from __future__ import annotations


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
