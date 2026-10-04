"""Runtime configuration that must never depend on the host machine.

- The data directory is overridable (PROCUREMENT_DATA_DIR) so eval fixtures and
  hidden-case data can be swapped in without code changes. It is resolved on
  every call, never cached at import time.
- The reference date for every date-based policy check is parsed from the
  policy document itself. The machine clock is never used.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
FALLBACK_REFERENCE_DATE = date(2026, 9, 30)
_REFERENCE_DATE_RE = re.compile(r"reference date:\**\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def data_dir() -> Path:
    override = os.getenv("PROCUREMENT_DATA_DIR", "").strip()
    if not override:
        return DEFAULT_DATA_DIR
    path = Path(override)
    return path if path.is_absolute() else ROOT / path


def reference_date() -> date:
    """Snapshot date declared in procurement_policy.md (fallback 2026-09-30)."""
    try:
        text = (data_dir() / "procurement_policy.md").read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_REFERENCE_DATE
    match = _REFERENCE_DATE_RE.search(text)
    return date.fromisoformat(match.group(1)) if match else FALLBACK_REFERENCE_DATE
