"""Tests for pipeline checkpoint/resume functionality."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.pipeline_support import (
    PipelineContext,
    PipelineStep,
    execute,
    load_checkpoint,
    save_checkpoint,
)
from src.storage import MemoryRunStorage


@pytest.fixture
def storage() -> MemoryRunStorage:
    return MemoryRunStorage("test/")


class TestLoadCheckpoint:
    def test_missing_file_returns_empty(self, storage: MemoryRunStorage):
        assert load_checkpoint(storage) == set()

    def test_loads_completed_steps(self, storage: MemoryRunStorage):
        storage.write_text(
            "checkpoint.json",
            json.dumps({"completed": ["intake", "validate", "plan"]}),
        )
        assert load_checkpoint(storage) == {"intake", "validate", "plan"}

    def test_corrupt_json_returns_empty(self, storage: MemoryRunStorage):
        storage.write_text("checkpoint.json", "not json")
        assert load_checkpoint(storage) == set()

    def test_empty_completed_returns_empty(self, storage: MemoryRunStorage):
        storage.write_text("checkpoint.json", json.dumps({"completed": []}))
        assert load_checkpoint(storage) == set()


class TestSaveCheckpoint:
    def test_creates_file(self, storage: MemoryRunStorage):
        save_checkpoint(storage, "intake")
        data = json.loads(storage.read_text("checkpoint.json"))
        assert data["completed"] == ["intake"]

    def test_appends_step(self, storage: MemoryRunStorage):
        save_checkpoint(storage, "intake")
        save_checkpoint(storage, "validate")
        data = json.loads(storage.read_text("checkpoint.json"))
        assert data["completed"] == ["intake", "validate"]

    def test_no_duplicates(self, storage: MemoryRunStorage):
        save_checkpoint(storage, "intake")
        save_checkpoint(storage, "intake")
        data = json.loads(storage.read_text("checkpoint.json"))
        assert data["completed"] == ["intake"]


class TestExecuteWithCheckpoint:
    def _make_ctx(self, storage: MemoryRunStorage) -> PipelineContext:
        return PipelineContext(
            worker=MagicMock(),
            async_worker=None,
            writer=MagicMock(),
            reviewer=MagicMock(),
            storage=storage,
            config=MagicMock(),
        )

    async def test_skips_completed_steps(self, storage: MemoryRunStorage):
        ctx = self._make_ctx(storage)
        calls = []
        steps = [
            PipelineStep("a", lambda _ctx: calls.append("a")),
            PipelineStep("b", lambda _ctx: calls.append("b")),
            PipelineStep("c", lambda _ctx: calls.append("c")),
        ]
        await execute(steps, ctx, checkpoint={"a", "b"})
        assert calls == ["c"]

    async def test_no_checkpoint_runs_all(self, storage: MemoryRunStorage):
        ctx = self._make_ctx(storage)
        calls = []
        steps = [
            PipelineStep("a", lambda _ctx: calls.append("a")),
            PipelineStep("b", lambda _ctx: calls.append("b")),
        ]
        await execute(steps, ctx)
        assert calls == ["a", "b"]

    async def test_writes_checkpoint_after_each_step(self, storage: MemoryRunStorage):
        ctx = self._make_ctx(storage)
        steps = [
            PipelineStep("a", lambda _ctx: None),
            PipelineStep("b", lambda _ctx: None),
        ]
        await execute(steps, ctx)
        checkpoint = load_checkpoint(storage)
        assert checkpoint == {"a", "b"}

    async def test_failed_step_not_checkpointed(self, storage: MemoryRunStorage):
        ctx = self._make_ctx(storage)

        def _fail(_ctx):
            raise RuntimeError("boom")

        steps = [
            PipelineStep("a", lambda _ctx: None),
            PipelineStep("b", _fail),
        ]
        with pytest.raises(RuntimeError, match="boom"):
            await execute(steps, ctx)

        checkpoint = load_checkpoint(storage)
        assert checkpoint == {"a"}

    async def test_empty_checkpoint_runs_all(self, storage: MemoryRunStorage):
        ctx = self._make_ctx(storage)
        calls = []
        steps = [
            PipelineStep("a", lambda _ctx: calls.append("a")),
        ]
        await execute(steps, ctx, checkpoint=set())
        assert calls == ["a"]
