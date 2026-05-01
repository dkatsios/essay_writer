"""Web logging bootstrap and per-run file logging utilities."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import load_config

_run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_CONSOLE_HANDLER_MARKER = "_essay_web_console_handler"
_RUN_HANDLER_MARKER = "_essay_run_file_handler"


class _LoggerNamePrefixFilter(logging.Filter):
    """Exclude records for logger names that start with a configured prefix."""

    def __init__(self, *blocked_prefixes: str) -> None:
        super().__init__()
        self.blocked_prefixes = tuple(prefix for prefix in blocked_prefixes if prefix)

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(
            record.name == prefix or record.name.startswith(f"{prefix}.")
            for prefix in self.blocked_prefixes
        )


class _RunFilter(logging.Filter):
    """Only pass records emitted in the current run context."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        return _run_id_var.get() == self.run_id


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line for structured log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": _run_id_var.get(),
        }
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exc_info"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False, default=str)


def _use_json_logging() -> bool:
    return load_config().web_log_format != "text"


def _ensure_src_logger_debug() -> logging.Logger:
    src_logger = logging.getLogger("src")
    if src_logger.level == logging.NOTSET or src_logger.level > logging.DEBUG:
        src_logger.setLevel(logging.DEBUG)
    return src_logger


def configure_web_logging() -> None:
    """Attach a single console handler for application logs in web mode."""
    src_logger = _ensure_src_logger_debug()
    for handler in src_logger.handlers:
        if getattr(handler, _CONSOLE_HANDLER_MARKER, False):
            return

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    if _use_json_logging():
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATEFMT))
    setattr(handler, _CONSOLE_HANDLER_MARKER, True)
    src_logger.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def set_run_id(run_id: str) -> None:
    _run_id_var.set(run_id)


def clear_run_id() -> None:
    _run_id_var.set(None)


@contextmanager
def run_id_context(run_id: str):
    """Bind a run id to the current context for the duration of the block."""
    token = _run_id_var.set(run_id)
    try:
        yield
    finally:
        _run_id_var.reset(token)


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
    handler.addFilter(_LoggerNamePrefixFilter("uvicorn.access"))
    setattr(handler, _RUN_HANDLER_MARKER, True)

    logging.getLogger().addHandler(handler)
    _ensure_src_logger_debug()
    return handler


def teardown_run_logging(handler: logging.FileHandler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()
