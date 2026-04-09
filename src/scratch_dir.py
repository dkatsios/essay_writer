"""Default run directory for local CLI use (no ``--dump-run``)."""

from __future__ import annotations

from pathlib import Path

# Used by ``src.runner`` when no timestamped ``--dump-run`` output dir is set.
SCRATCH_RUN_DIR = Path(".output/scratch")
