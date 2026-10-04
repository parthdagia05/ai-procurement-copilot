from __future__ import annotations

import os

from src.contracts import Architecture, ProcurementDecision
from src.decision import build_deterministic
from src.runtime import RunContext


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter.

    single  -> Architecture A (one tool-calling agent)
    staged  -> Architecture B (analyst agent -> policy/risk reviewer agent)
    Both sit on the same deterministic policy floor. With no LLM key configured
    (or COPILOT_MODE=deterministic) the deterministic engine answers alone.
    """
    if architecture not in ("single", "staged"):
        raise ValueError(f"Unknown architecture '{architecture}' (expected 'single' or 'staged')")
    ctx = RunContext(request_id=request_id, architecture=architecture)
    ctx.telemetry.fallback_reason = "deterministic_mode" if os.getenv("COPILOT_MODE") == "deterministic" else "llm_not_configured"
    return build_deterministic(ctx)
