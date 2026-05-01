"""Start multiple worker processes from one Python entrypoint."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start one or more essay worker processes.",
    )
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=int(os.environ.get("ESSAY_WORKER_COUNT", "6")),
        help="Number of worker processes to start (default: 6 or ESSAY_WORKER_COUNT).",
    )
    return parser.parse_args()


def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()


def main() -> int:
    args = _parse_args()
    if args.count < 1:
        print("worker count must be at least 1", file=sys.stderr)
        return 1

    processes = [
        subprocess.Popen([sys.executable, "-m", "src.worker"])
        for _ in range(args.count)
    ]

    def _handle_signal(signum, frame) -> None:  # type: ignore[unused-argument]
        _terminate(processes)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while True:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    _terminate(processes)
                    for other in processes:
                        try:
                            other.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            other.kill()
                    return exit_code if exit_code != 0 else 1
            time.sleep(1)
    finally:
        _terminate(processes)


if __name__ == "__main__":
    raise SystemExit(main())
