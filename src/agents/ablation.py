"""ABLATION ONLY (evals): Architecture A without the deterministic floor.

Same model, same data tools and follow-up tools, but no policy engine: the
model reads the policy text and decides approvals, flags, missing information
and even human_review_required itself, and its output is used as-is (no merge,
no sanitiser). Measures what the floor contributes. Never used by the product.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src import data_access
from src.agents.common import agent_turns, evidence_messages
from src.agents.prompts import UNTRUSTED_RULES
from src.contracts import ProcurementDecision
from src.decision import LABEL_TEXT
from src.llm.client import LLMClient
from src.rules import FLAGS, LABELS, ROLES
from src.runtime import RunContext

DATA_TOOLS = ["get_request_context", "check_budget", "search_existing_software", "get_vendor_profile"]


class RawDecision(BaseModel):
    recommendation_label: str
    required_approvals: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    rationale: str = ""
    next_step: str = ""
    human_review_required: bool = True


def _schema() -> dict:
    schema = RawDecision.model_json_schema()
    schema["properties"]["recommendation_label"]["enum"] = LABELS
    schema["properties"]["required_approvals"]["items"]["enum"] = ROLES
    schema["properties"]["risk_flags"]["items"]["enum"] = FLAGS
    return schema


def run_unguarded(ctx: RunContext, llm: LLMClient) -> ProcurementDecision:
    system = f"""You are the Procurement Request Copilot. Review ONE software purchase request and recommend the next action to a human procurement reviewer. Apply the procurement policy below yourself: decide the required approvals, risk flags and missing information.

{UNTRUSTED_RULES}

POLICY:
{data_access.load_policy_text()}"""
    messages = evidence_messages(ctx, system, f"Review purchase request {ctx.request_id}.", DATA_TOOLS)
    instruction = (f"Reply with ONLY a JSON object matching the schema below. recommendation_label: one of {LABELS}. "
                   f"required_approvals from {ROLES}. risk_flags from {FLAGS}. missing_information: policy s1 fields that are missing.")
    raw = agent_turns(ctx, llm, messages, RawDecision, _schema(), instruction)
    ctx.memo["assessment"] = raw.model_dump()
    label = raw.recommendation_label if raw.recommendation_label in LABELS else "hold_for_manual_review"
    return ProcurementDecision(
        request_id=ctx.request_id,
        recommendation=f"{label}: {LABEL_TEXT[label]}. {raw.rationale}",
        evidence=list(ctx.ledger.values()),
        required_approvals=[r for r in ROLES if r in raw.required_approvals],
        missing_information=raw.missing_information,
        risk_flags=[f for f in FLAGS if f in raw.risk_flags],
        next_step=raw.next_step,
        human_review_required=raw.human_review_required,
        telemetry=ctx.telemetry.to_contract(ctx.elapsed_ms()),
    )
