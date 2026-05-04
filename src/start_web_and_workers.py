"""Start the web server and worker launcher in one container process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from config.settings import load_config


def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()


def main() -> int:
    config = load_config()
    port = os.environ.get("PORT", "8000")
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "src.start_workers", str(config.worker_count)]
        ),
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "src.web:app",
                "--host",
                "0.0.0.0",
                "--port",
                port,
            ]
        ),
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
                        if other is process:
                            continue
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
