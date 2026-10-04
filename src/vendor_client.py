from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import requests

VendorRiskStatus = Literal["ok", "not_found", "unavailable"]


@dataclass
class VendorRiskResult:
    """Typed outcome of a vendor-risk lookup.

    ok          - the service returned an assessment record
    not_found   - the service has no assessment for this vendor (404): the assessment is MISSING
    unavailable - the service could not be reached or failed (5xx / timeout / refused / bad body)
    """
    status: VendorRiskStatus
    endpoint: str
    data: dict | None = None
    reason: str | None = None
    http_status: int | None = None
    attempts: int = 0
    latency_ms: float = 0.0


def base_url() -> str:
    return os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def _encode_vendor(vendor_name: str) -> str:
    # The mock API calls unquote() on a path parameter the framework has already
    # decoded, so a name containing '%' or '/' is decoded twice. Encode those names
    # twice so they survive; plain names are encoded once (identical either way).
    encoded = quote(vendor_name, safe="")
    if "%" in vendor_name or "/" in vendor_name:
        encoded = quote(encoded, safe="")
    return encoded


def fetch_vendor_risk(
    vendor_name: str,
    timeout_seconds: float = 3.0,
    retries: int = 1,
    backoff_seconds: float = 0.5,
) -> VendorRiskResult:
    """Never raises. Retries only transient failures (5xx, timeout, connection)."""
    url = f"{base_url()}/vendor-risk/{_encode_vendor(vendor_name)}"
    result = VendorRiskResult(status="unavailable", endpoint=f"GET /vendor-risk/{vendor_name}")
    start = time.perf_counter()
    for attempt in range(1, retries + 2):
        result.attempts = attempt
        transient = False
        try:
            response = requests.get(url, timeout=timeout_seconds)
        except requests.Timeout:
            result.reason, transient = "timeout", True
        except requests.ConnectionError:
            result.reason, transient = "connection_refused", True
        else:
            result.http_status = response.status_code
            if response.status_code == 200:
                try:
                    result.data = response.json()
                    result.status, result.reason = "ok", None
                except ValueError:
                    result.reason = "invalid_json"
            elif response.status_code == 404:
                result.status, result.reason = "not_found", "no_assessment_record"
            else:
                result.reason = f"http_{response.status_code}"
                transient = response.status_code >= 500
        if not transient or attempt > retries:
            break
        time.sleep(backoff_seconds * attempt)
    result.latency_ms = round((time.perf_counter() - start) * 1000, 1)
    return result


def get_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Starter-pack API, kept for compatibility: raises on any non-200 response."""
    url = f"{base_url()}/vendor-risk/{_encode_vendor(vendor_name)}"
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()
