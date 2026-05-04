#!/bin/sh

set -eu

if [ "$#" -gt 0 ]; then
  exec uv run python -m src.start_workers "$1"
fi

exec uv run python -m src.start_workers
