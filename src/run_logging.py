"""Per-run file logging with async-safe context isolation for concurrent jobs."""

from __future__ import annotations

import logging
from contextvars import ContextVar, copy_context
from pathlib import Path

_run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _RunFilter(logging.Filter):
    """Only pass records emitted in the current run context."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        return _run_id_var.get() == self.run_id


def set_run_id(run_id: str) -> None:
    _run_id_var.set(run_id)


def clear_run_id() -> None:
    _run_id_var.set(None)


def submit_with_current_context(executor, fn, /, *args, **kwargs):
    """Submit work to an executor while preserving current context vars."""
    ctx = copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


def setup_run_logging(run_dir: Path, run_id: str) -> logging.FileHandler:
    """Create a per-run log file at *run_dir/run.log* filtered to *run_id*."""
    log_path = run_dir / "run.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATEFMT))
    handler.addFilter(_RunFilter(run_id))
    src_logger = logging.getLogger("src")
    src_logger.addHandler(handler)
    # Ensure INFO+ records reach the handler even when the root logger is at
    # WARNING (e.g. web UI where basicConfig is not called by our code).
    if src_logger.level == logging.NOTSET or src_logger.level > logging.DEBUG:
        src_logger.setLevel(logging.DEBUG)
    return handler


def teardown_run_logging(handler: logging.FileHandler) -> None:
    logging.getLogger("src").removeHandler(handler)
    handler.close()
