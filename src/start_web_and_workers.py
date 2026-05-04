"""Start the web server and worker launcher in one container process."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

from config.settings import load_config


def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()


def _wait_for_port(port: int, *, timeout_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> int:
    config = load_config()
    port = os.environ.get("PORT", "8000")
    web_process = subprocess.Popen(
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
    )
    processes = [web_process]

    try:
        port_number = int(port)
    except ValueError:
        _terminate(processes)
        return 1

    if not _wait_for_port(port_number):
        _terminate(processes)
        try:
            web_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            web_process.kill()
        return web_process.returncode or 1

    worker_process = subprocess.Popen(
        [sys.executable, "-m", "src.start_workers", str(config.worker_count)]
    )
    processes.append(worker_process)

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
