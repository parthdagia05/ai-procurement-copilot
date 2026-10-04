from __future__ import annotations

import os

from src.contracts import Architecture, ProcurementDecision
from src.decision import build_decision
from src.llm.client import LLMClient, LLMError
from src.runtime import RunContext
from src.tools import MANDATORY_TOOLS, base_policy, run_tool


def _deterministic(ctx: RunContext, reason: str) -> ProcurementDecision:
    """Policy-engine-only decision. Also the fallback whenever the LLM fails."""
    ctx.telemetry.fallback_reason = reason
    for name in MANDATORY_TOOLS:
        if name not in ctx.telemetry.tool_names:
            run_tool(ctx, name, {})
    return build_decision(ctx, base_policy(ctx))


def run(request_id: str, architecture: str = "single", *, llm: LLMClient | None = None,
        mode: str | None = None, cache_tag: str = "") -> tuple[ProcurementDecision, RunContext]:
    """Internal entry point that also returns the run context (trace, merge notes) for evals and the UI.

    mode: None (LLM if configured) | "deterministic" (policy engine only) | "unguarded" (ablation, evals only)
    """
    if architecture not in ("single", "staged"):
        raise ValueError(f"Unknown architecture '{architecture}' (expected 'single' or 'staged')")
    ctx = RunContext(request_id=request_id, architecture=architecture)
    ctx.memo["cache_tag"] = cache_tag
    mode = mode or os.getenv("COPILOT_MODE") or None
    if mode == "deterministic":
        return _deterministic(ctx, "deterministic_mode"), ctx
    llm = llm or LLMClient()
    if not llm.available:
        return _deterministic(ctx, "llm_not_configured"), ctx
    try:
        if mode == "unguarded":
            from src.agents.ablation import run_unguarded
            return run_unguarded(ctx, llm), ctx
        if architecture == "single":
            from src.agents.single import run_single
            return run_single(ctx, llm), ctx
        from src.agents.staged import run_staged
        return run_staged(ctx, llm), ctx
    except LLMError as exc:
        ctx.log("llm_failure", error=str(exc)[:300])
        return _deterministic(ctx, f"llm_error: {type(exc).__name__}: {str(exc)[:120]}"), ctx


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter.

    single -> Architecture A: one tool-calling agent
    staged -> Architecture B: analyst agent -> policy/risk reviewer agent
    Both sit on the same deterministic policy floor; without an LLM key (or with
    COPILOT_MODE=deterministic) the policy engine answers alone.
    """
    return run(request_id, architecture)[0]
