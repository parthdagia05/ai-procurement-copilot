"""Make sure a vendor-risk API is reachable before running evals.

Without this, a stopped mock API looks exactly like a vendor-risk outage and
every case is silently scored as degraded.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests

from src.config import ROOT
from src.vendor_client import base_url


def is_healthy(url: str) -> bool:
    try:
        return requests.get(f"{url}/health", timeout=0.5).ok
    except requests.RequestException:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def ensure_mock_api(data_dir: Path | None = None) -> Iterator[str]:
    """Yield a base URL serving vendor-risk data.

    data_dir=None -> reuse the configured API if healthy, else start one.
    data_dir set  -> always start a private instance on a free port serving that
                     directory (a running server's data dir cannot be verified).
    VENDOR_RISK_BASE_URL is pointed at the instance for the duration.
    """
    configured = base_url()
    if data_dir is None and is_healthy(configured):
        yield configured
        return

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    if data_dir is not None:
        env["PROCUREMENT_DATA_DIR"] = str(data_dir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT,
        env=env,
    )
    previous = os.environ.get("VENDOR_RISK_BASE_URL")
    try:
        deadline = time.monotonic() + 15
        while not is_healthy(url):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Could not start mock vendor-risk API on {url}")
            time.sleep(0.1)
        os.environ["VENDOR_RISK_BASE_URL"] = url
        yield url
    finally:
        if previous is None:
            os.environ.pop("VENDOR_RISK_BASE_URL", None)
        else:
            os.environ["VENDOR_RISK_BASE_URL"] = previous
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
