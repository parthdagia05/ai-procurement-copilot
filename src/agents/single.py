"""Architecture A: one agent on top of the deterministic floor.

Flow: mandatory evidence tools (code) -> agent turn(s) with follow-up tools ->
JSON assessment -> merge with the floor. Typical LLM calls: 1 (2-3 with follow-ups or a repair).
"""
from __future__ import annotations

from src.agents.common import agent_turns, evidence_messages
from src.agents.prompts import SINGLE_FINAL, SINGLE_SYSTEM
from src.contracts import ProcurementDecision
from src.decision import AgentAssessment, assessment_json_schema, build_decision
from src.llm.client import LLMClient
from src.runtime import RunContext
from src.tools import base_policy


def run_single(ctx: RunContext, llm: LLMClient) -> ProcurementDecision:
    messages = evidence_messages(ctx, SINGLE_SYSTEM, f"Review purchase request {ctx.request_id}.")
    policy = base_policy(ctx)
    assessment = agent_turns(ctx, llm, messages, AgentAssessment, assessment_json_schema(),
                             SINGLE_FINAL.format(allowed=policy.allowed_labels))
    ctx.memo["assessment"] = assessment.model_dump()
    return build_decision(ctx, policy, assessment)
