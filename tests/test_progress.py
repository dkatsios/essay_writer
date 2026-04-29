"""Tests for fine-grained UI progress tracking."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import src.runtime as runtime_module

from src.runtime import TokenTracker
from src.pipeline_support import PipelineContext, PipelineStep, execute
from src.web_jobs import Job, build_status_payload


class TestTokenTrackerProgress:
    def test_initial_values(self):
        tracker = TokenTracker()
        assert tracker.step_index == 0
        assert tracker.step_count == 0
        assert tracker.sub_done == 0
        assert tracker.sub_total == 0

    def test_set_step_progress(self):
        tracker = TokenTracker()
        tracker.set_step_progress(3, 8)
        assert tracker.step_index == 3
        assert tracker.step_count == 8

    def test_set_sub_total_resets_done(self):
        tracker = TokenTracker()
        tracker.set_sub_total(10)
        tracker.increment_sub_done()
        tracker.increment_sub_done()
        assert tracker.sub_done == 2
        tracker.set_sub_total(5)
        assert tracker.sub_done == 0
        assert tracker.sub_total == 5

    def test_increment_sub_done(self):
        tracker = TokenTracker()
        tracker.set_sub_total(3)
        tracker.increment_sub_done()
        assert tracker.sub_done == 1
        tracker.increment_sub_done()
        assert tracker.sub_done == 2
        tracker.increment_sub_done()
        assert tracker.sub_done == 3

    def test_on_progress_callback_fires(self):
        tracker = TokenTracker()
        calls = []
        tracker.set_on_progress(lambda: calls.append(1))
        tracker.set_sub_total(5)
        tracker.increment_sub_done()
        tracker.increment_sub_done()
        assert len(calls) == 2

    def test_on_progress_none_does_not_crash(self):
        tracker = TokenTracker()
        tracker.set_on_progress(None)
        tracker.set_sub_total(5)
        tracker.increment_sub_done()
        assert tracker.sub_done == 1

    def test_thread_safety(self):
        tracker = TokenTracker()
        tracker.set_sub_total(1000)
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            for _ in range(250):
                tracker.increment_sub_done()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert tracker.sub_done == 1000

    def test_cost_summary_skips_calc_for_empty_non_llm_steps(self, monkeypatch):
        tracker = TokenTracker()
        tracker.record_duration("research", 14.0)
        calls: list[str] = []

        def fake_calc_cost(model, input_tokens, output_tokens, thinking_tokens=0):
            calls.append(model)
            return 0.0

        monkeypatch.setattr(runtime_module, "calc_cost", fake_calc_cost)

        summary = tracker.cost_summary()

        assert "research" in summary
        assert "unknown" in summary
        assert calls == []


class TestExecuteStepProgress:
    async def test_step_progress_set_on_tracker(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        tracker = TokenTracker()
        ctx = PipelineContext(
            worker=MagicMock(),
            async_worker=None,
            writer=MagicMock(),
            reviewer=MagicMock(),
            run_dir=run_dir,
            config=MagicMock(),
            tracker=tracker,
        )
        captured = []

        def step_fn(_ctx):
            captured.append((tracker.step_index, tracker.step_count))

        steps = [
            PipelineStep("a", step_fn),
            PipelineStep("b", step_fn),
        ]
        await execute(steps, ctx, step_offset=2, total_steps=5)
        assert captured == [(2, 5), (3, 5)]

    async def test_sub_total_reset_between_steps(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        tracker = TokenTracker()
        ctx = PipelineContext(
            worker=MagicMock(),
            async_worker=None,
            writer=MagicMock(),
            reviewer=MagicMock(),
            run_dir=run_dir,
            config=MagicMock(),
            tracker=tracker,
        )
        sub_totals_at_start = []

        def step_one(_ctx):
            tracker.set_sub_total(10)
            tracker.increment_sub_done()
            tracker.increment_sub_done()

        def step_two(_ctx):
            sub_totals_at_start.append((tracker.sub_total, tracker.sub_done))

        steps = [
            PipelineStep("s1", step_one),
            PipelineStep("s2", step_two),
        ]
        await execute(steps, ctx, step_offset=0, total_steps=2)
        assert sub_totals_at_start == [(0, 0)]

    async def test_no_total_steps_skips_progress(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        tracker = TokenTracker()
        ctx = PipelineContext(
            worker=MagicMock(),
            async_worker=None,
            writer=MagicMock(),
            reviewer=MagicMock(),
            run_dir=run_dir,
            config=MagicMock(),
            tracker=tracker,
        )

        def noop(_ctx):
            pass

        await execute([PipelineStep("x", noop)], ctx)
        # step_count stays at default 0 when total_steps not provided
        assert tracker.step_count == 0


class TestBuildStatusPayloadProgress:
    def test_includes_step_progress(self):
        tracker = TokenTracker()
        tracker.set_current_step("research")
        tracker.set_step_progress(3, 8)
        job = Job(job_id="progressjob01", run_dir=Path("/tmp"), status="running")
        job.tracker = tracker
        payload = build_status_payload(job)
        assert payload["step_index"] == 3
        assert payload["step_count"] == 8
        assert "sub_done" not in payload
        assert "sub_total" not in payload

    def test_includes_sub_progress(self):
        tracker = TokenTracker()
        tracker.set_current_step("read_sources:fetch")
        tracker.set_step_progress(4, 9)
        tracker.set_sub_total(45)
        for _ in range(12):
            tracker.increment_sub_done()
        job = Job(job_id="subprogjob01", run_dir=Path("/tmp"), status="running")
        job.tracker = tracker
        payload = build_status_payload(job)
        assert payload["sub_done"] == 12
        assert payload["sub_total"] == 45

    def test_no_progress_when_not_running(self):
        tracker = TokenTracker()
        tracker.set_step_progress(5, 8)
        tracker.set_sub_total(10)
        job = Job(job_id="doneprogjo01", run_dir=Path("/tmp"), status="done")
        job.tracker = tracker
        payload = build_status_payload(job)
        assert "step_index" not in payload
        assert "sub_total" not in payload
