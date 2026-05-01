#!/bin/sh

set -eu

WEB_PORT=${PORT:-8000}

uv run python -m src.worker &
worker_pid=$!

uv run uvicorn src.web:app --host 0.0.0.0 --port "$WEB_PORT" &
web_pid=$!

cleanup() {
  kill "$worker_pid" "$web_pid" 2>/dev/null || true
}

trap cleanup INT TERM

while kill -0 "$worker_pid" 2>/dev/null && kill -0 "$web_pid" 2>/dev/null; do
  sleep 1
done

cleanup

wait "$worker_pid" 2>/dev/null || true
wait "$web_pid" 2>/dev/null || true

exit 1
