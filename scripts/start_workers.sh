#!/bin/sh

set -eu

WORKER_COUNT=${1:-${ESSAY_WORKER_COUNT:-6}}

case "$WORKER_COUNT" in
  ''|*[!0-9]*)
    echo "worker count must be a positive integer" >&2
    exit 1
    ;;
esac

if [ "$WORKER_COUNT" -lt 1 ]; then
  echo "worker count must be at least 1" >&2
  exit 1
fi

worker_pids=""
worker_index=1

while [ "$worker_index" -le "$WORKER_COUNT" ]; do
  uv run python -m src.worker &
  worker_pid=$!
  worker_pids="$worker_pids $worker_pid"
  worker_index=$((worker_index + 1))
done

cleanup() {
  for pid in $worker_pids; do
    kill "$pid" 2>/dev/null || true
  done
}

trap cleanup INT TERM

while :; do
  for pid in $worker_pids; do
    if ! kill -0 "$pid" 2>/dev/null; then
      cleanup
      for wait_pid in $worker_pids; do
        wait "$wait_pid" 2>/dev/null || true
      done
      exit 1
    fi
  done
  sleep 1
done
