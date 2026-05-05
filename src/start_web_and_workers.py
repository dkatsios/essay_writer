"""Start the web server and worker launcher in one container process."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

from config.settings import load_config
from src.db_upgrade import upgrade_database


def _log(message: str) -> None:
    print(f"[combined-startup] {message}", flush=True)


def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            _log(f"terminating child pid={process.pid}")
            process.terminate()


def _run_migrations() -> int:
    _log("running database upgrade")
    return upgrade_database(status=print)


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
    _log(
        "starting combined entrypoint "
        f"port={port} worker_count={config.worker_count} web_only={config.combined_web_only}"
    )

    migration_exit_code = _run_migrations()
    if migration_exit_code != 0:
        _log(f"database upgrade failed exit_code={migration_exit_code}")
        return migration_exit_code

    _log("starting web process")
    web_process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "uvicorn",
            "src.web:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ]
    )
    _log(f"web process pid={web_process.pid}")
    processes = [web_process]

    try:
        port_number = int(port)
    except ValueError:
        _log(f"invalid port value {port!r}")
        _terminate(processes)
        return 1

    _log(f"waiting for web port bind port={port_number}")
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        exit_code = web_process.poll()
        if exit_code is not None:
            _log(f"web process exited before bind exit_code={exit_code}")
            return exit_code if exit_code != 0 else 1
        if _wait_for_port(port_number, timeout_seconds=0.2):
            break
    else:
        _log("web port did not bind before timeout")
        _terminate(processes)
        try:
            web_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            web_process.kill()
        return web_process.returncode or 1

    _log("web port bound successfully")

    if config.combined_web_only:
        _log("combined_web_only enabled; skipping worker startup")
    else:
        _log(f"starting worker launcher count={config.worker_count}")
        worker_process = subprocess.Popen(
            [sys.executable, "-m", "src.start_workers", str(config.worker_count)]
        )
        _log(f"worker launcher pid={worker_process.pid}")
        processes.append(worker_process)

    def _handle_signal(signum, frame) -> None:  # type: ignore[unused-argument]
        _log(f"received signal={signum}")
        _terminate(processes)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while True:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    _log(
                        f"child process exited pid={process.pid} exit_code={exit_code}"
                    )
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
