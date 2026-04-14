"""Tests for pipeline checkpoint/resume functionality."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.pipeline_support import (
    PipelineContext,
    PipelineStep,
    _execute,
    _load_checkpoint,
    _save_checkpoint,
)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Create a temporary run directory."""
    return tmp_path / "run"


class TestLoadCheckpoint:
    def test_missing_file_returns_empty(self, run_dir: Path):
        assert _load_checkpoint(run_dir) == set()

    def test_loads_completed_steps(self, run_dir: Path):
        run_dir.mkdir(parents=True)
        (run_dir / "checkpoint.json").write_text(
            json.dumps({"completed": ["intake", "validate", "plan"]}),
            encoding="utf-8",
        )
        assert _load_checkpoint(run_dir) == {"intake", "validate", "plan"}

    def test_corrupt_json_returns_empty(self, run_dir: Path):
        run_dir.mkdir(parents=True)
        (run_dir / "checkpoint.json").write_text("not json", encoding="utf-8")
        assert _load_checkpoint(run_dir) == set()

    def test_empty_completed_returns_empty(self, run_dir: Path):
        run_dir.mkdir(parents=True)
        (run_dir / "checkpoint.json").write_text(
            json.dumps({"completed": []}), encoding="utf-8"
        )
        assert _load_checkpoint(run_dir) == set()


class TestSaveCheckpoint:
    def test_creates_file(self, run_dir: Path):
        run_dir.mkdir(parents=True)
        _save_checkpoint(run_dir, "intake")
        data = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        assert data["completed"] == ["intake"]

    def test_appends_step(self, run_dir: Path):
        run_dir.mkdir(parents=True)
        _save_checkpoint(run_dir, "intake")
        _save_checkpoint(run_dir, "validate")
        data = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        assert data["completed"] == ["intake", "validate"]

    def test_no_duplicates(self, run_dir: Path):
        run_dir.mkdir(parents=True)
        _save_checkpoint(run_dir, "intake")
        _save_checkpoint(run_dir, "intake")
        data = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        assert data["completed"] == ["intake"]


class TestExecuteWithCheckpoint:
    def _make_ctx(self, run_dir: Path) -> PipelineContext:
        run_dir.mkdir(parents=True, exist_ok=True)
        return PipelineContext(
            worker=MagicMock(),
            async_worker=None,
            writer=MagicMock(),
            reviewer=MagicMock(),
            run_dir=run_dir,
            config=MagicMock(),
        )

    def test_skips_completed_steps(self, run_dir: Path):
        ctx = self._make_ctx(run_dir)
        calls = []
        steps = [
            PipelineStep("a", lambda _ctx: calls.append("a")),
            PipelineStep("b", lambda _ctx: calls.append("b")),
            PipelineStep("c", lambda _ctx: calls.append("c")),
        ]
        _execute(steps, ctx, checkpoint={"a", "b"})
        assert calls == ["c"]

    def test_no_checkpoint_runs_all(self, run_dir: Path):
        ctx = self._make_ctx(run_dir)
        calls = []
        steps = [
            PipelineStep("a", lambda _ctx: calls.append("a")),
            PipelineStep("b", lambda _ctx: calls.append("b")),
        ]
        _execute(steps, ctx)
        assert calls == ["a", "b"]

    def test_writes_checkpoint_after_each_step(self, run_dir: Path):
        ctx = self._make_ctx(run_dir)
        steps = [
            PipelineStep("a", lambda _ctx: None),
            PipelineStep("b", lambda _ctx: None),
        ]
        _execute(steps, ctx)
        checkpoint = _load_checkpoint(run_dir)
        assert checkpoint == {"a", "b"}

    def test_failed_step_not_checkpointed(self, run_dir: Path):
        ctx = self._make_ctx(run_dir)

        def _fail(_ctx):
            raise RuntimeError("boom")

        steps = [
            PipelineStep("a", lambda _ctx: None),
            PipelineStep("b", _fail),
        ]
        with pytest.raises(RuntimeError, match="boom"):
            _execute(steps, ctx)

        checkpoint = _load_checkpoint(run_dir)
        assert checkpoint == {"a"}

    def test_empty_checkpoint_runs_all(self, run_dir: Path):
        ctx = self._make_ctx(run_dir)
        calls = []
        steps = [
            PipelineStep("a", lambda _ctx: calls.append("a")),
        ]
        _execute(steps, ctx, checkpoint=set())
        assert calls == ["a"]
