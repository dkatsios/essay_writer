"""Tests for writer auth endpoints and job ownership."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.web import Job, _jobs, _save_job, app
from src.writer_store import writer_store

client = TestClient(app)


def _login(email: str = "test@example.com", name: str = "Test") -> dict:
    """Login helper that returns the writer dict and sets cookies on client."""
    resp = client.post("/login", json={"email": email, "name": name})
    assert resp.status_code == 200
    return resp.json()


def test_login_creates_writer():
    resp = client.post(
        "/login", json={"email": "auth_new@test.com", "name": "Auth New"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "auth_new@test.com"
    assert data["name"] == "Auth New"
    assert len(data["id"]) == 32


def test_login_idempotent():
    r1 = client.post("/login", json={"email": "auth_idem@test.com", "name": "Idem"})
    r2 = client.post("/login", json={"email": "auth_idem@test.com", "name": "Idem"})
    assert r1.json()["id"] == r2.json()["id"]


def test_login_requires_email():
    resp = client.post("/login", json={"email": "", "name": "X"})
    assert resp.status_code == 400


def test_me_without_login():
    # Use a fresh client without cookies
    fresh = TestClient(app)
    resp = fresh.get("/me")
    assert resp.status_code == 401


def test_me_after_login():
    resp = client.post("/login", json={"email": "me_test@test.com", "name": "Me"})
    assert resp.status_code == 200
    # The test client carries cookies automatically
    me_resp = client.get("/me")
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "me_test@test.com"


def test_logout_clears_session():
    client.post("/login", json={"email": "logout_test@test.com", "name": "Out"})
    resp = client.post("/logout")
    assert resp.status_code == 200
    # After logout, /me should fail
    me_resp = client.get("/me")
    assert me_resp.status_code == 401


def test_writers_list():
    writer_store.find_or_create("list_w1@test.com", "Writer One")
    writer_store.find_or_create("list_w2@test.com", "Writer Two")
    resp = client.get("/writers")
    assert resp.status_code == 200
    emails = [w["email"] for w in resp.json()["writers"]]
    assert "list_w1@test.com" in emails
    assert "list_w2@test.com" in emails


def test_submit_requires_login():
    fresh = TestClient(app)
    resp = fresh.post("/submit", data={"prompt": "Hello"})
    assert resp.status_code == 401


def test_submit_stamps_writer_id(monkeypatch):
    from src.storage import MemoryRunStorage

    class _Uuid:
        hex = "writer_submit_test_00"

    def fake_create(job_id, config=None):
        return MemoryRunStorage(f"runs/{job_id}/")

    monkeypatch.setattr("src.web.uuid.uuid4", lambda: _Uuid())
    monkeypatch.setattr("src.web.create_run_storage", fake_create)

    # Login first
    login_resp = client.post(
        "/login", json={"email": "submit_stamp@test.com", "name": "Stamper"}
    )
    writer_id = login_resp.json()["id"]
    resp = client.post("/submit", data={"prompt": "Test essay"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    job = _jobs.refresh(job_id)
    assert job is not None
    assert job.writer_id == writer_id


def test_transfer_requires_login():
    fresh = TestClient(app)
    resp = fresh.post("/history/jobs/someid/transfer", json={"writer_id": "x"})
    assert resp.status_code == 401


def test_transfer_requires_assigned_writer():
    # Create a job owned by writer A
    wa = writer_store.find_or_create("transfer_a@test.com", "Writer A")
    writer_store.find_or_create("transfer_b@test.com", "Writer B")

    job = Job(job_id="transfer_test1", run_dir="runs/transfer_test1/", writer_id=wa.id)
    _save_job(job)

    # Login as writer B and try to transfer — should be forbidden
    client.post("/login", json={"email": "transfer_b@test.com", "name": "Writer B"})
    resp = client.post(
        "/history/jobs/transfer_test1/transfer",
        json={"writer_id": wa.id},
    )
    assert resp.status_code == 403


def test_transfer_succeeds_for_assigned_writer():
    wa = writer_store.find_or_create("transfer_ok_a@test.com", "OK A")
    wb = writer_store.find_or_create("transfer_ok_b@test.com", "OK B")

    from src.run_history_store import run_history

    job = Job(job_id="transfer_test2", run_dir="runs/transfer_test2/", writer_id=wa.id)
    _save_job(job)
    run_history.save_runtime_summary(
        "transfer_test2", status="done", writer_id=wa.id, updated_at=1.0
    )

    # Login as writer A (the assigned writer)
    client.post("/login", json={"email": "transfer_ok_a@test.com", "name": "OK A"})
    resp = client.post(
        "/history/jobs/transfer_test2/transfer",
        json={"writer_id": wb.id},
    )
    assert resp.status_code == 200
    assert resp.json()["writer"]["id"] == wb.id

    # Verify job is updated
    refreshed = _jobs.refresh("transfer_test2")
    assert refreshed.writer_id == wb.id


def test_history_jobs_includes_writer_info():
    from src.run_history_store import run_history

    w = writer_store.find_or_create("history_w@test.com", "History W")
    run_history.save_runtime_summary(
        "hist_writer_test",
        status="done",
        writer_id=w.id,
        updated_at=99.0,
    )

    resp = client.get("/history/jobs")
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    target = next((j for j in jobs if j["job_id"] == "hist_writer_test"), None)
    assert target is not None
    assert target["writer_id"] == w.id
    assert target["writer_name"] == "History W"


def test_history_jobs_filter_by_writer_id():
    from src.run_history_store import run_history

    w1 = writer_store.find_or_create("filter_w1@test.com", "Filter W1")
    w2 = writer_store.find_or_create("filter_w2@test.com", "Filter W2")
    run_history.save_runtime_summary(
        "filter_job_w1", status="done", writer_id=w1.id, updated_at=50.0
    )
    run_history.save_runtime_summary(
        "filter_job_w2", status="done", writer_id=w2.id, updated_at=51.0
    )

    # Filter to only w1
    resp = client.get("/history/jobs", params={"writer_id": w1.id})
    assert resp.status_code == 200
    job_ids = [j["job_id"] for j in resp.json()["jobs"]]
    assert "filter_job_w1" in job_ids
    assert "filter_job_w2" not in job_ids
