"""Canonical directory for the latest non-timestamped pipeline run."""

from __future__ import annotations

from pathlib import Path

# CLI default when ``--dump-run`` is not used; web UI uses the same path so the
# last run is always inspectable under ``.output/scratch``.
SCRATCH_RUN_DIR = Path(".output/scratch")


def is_scratch_run_dir(run_dir: Path) -> bool:
    """True if ``run_dir`` is the shared scratch folder (do not rmtree on cleanup)."""
    try:
        return Path(run_dir).resolve() == SCRATCH_RUN_DIR.resolve()
    except OSError:
        return False
