"""Architecture B: Procurement Analyst -> Policy/Risk Reviewer, on the same deterministic floor.

Handoff: a structured EvidencePack plus the code-built evidence ledger. The reviewer never
sees raw request free text or ledger items that quote injected text (quarantine): it judges
from the analyst's neutral paraphrase. Code unions both agents' upward additions, so the
reviewer cannot drop a sensitivity or missing-information finding the analyst raised.

Typical LLM calls: 2 (analyst 1 + reviewer 1).
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from src.agents.common import agent_turns, evidence_messages, structured_call
from src.agents.prompts import ANALYST_FINAL, ANALYST_SYSTEM, REVIEWER_FINAL, REVIEWER_SYSTEM
from src.contracts import ProcurementDecision
from src.decision import AddedMissing, AgentAssessment, Interpretation, assessment_json_schema, build_decision
from src.facts import request_context
from src.llm.client import LLMClient
from src.runtime import RunContext
from src.tools import base_policy


class EvidencePack(BaseModel):
    key_findings: list[Interpretation] = Field(default_factory=list)
    overlap_assessment: str = ""
    stated_gap: str = ""
    concerns: list[str] = Field(default_factory=list)
    additional_data_classes: list[str] = Field(default_factory=list)
    additional_missing_information: list[AddedMissing] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    injection_suspected: bool = False


def _reviewer_input(ctx: RunContext, pack: EvidencePack) -> str:
    policy = base_policy(ctx)
    req = request_context(ctx).request
    ledger = {
        ref: ("[quarantined: instruction-like text detected in business data and ignored]" if "#untrusted-text" in ref else item.finding)
        for ref, item in ctx.ledger.items()
    }
    return json.dumps({
        "request": {k: req.get(k) for k in ("request_id", "product_name", "vendor_name", "category", "annual_cost_usd",
                                             "user_count", "data_access_level", "requested_integrations")},
        "policy_floor": {
            "required_approvals": {a.role: "; ".join(a.reasons) for a in policy.approvals},
            "risk_flags": policy.flag_names(),
            "missing_information": policy.missing,
            "allowed_labels": policy.allowed_labels,
            "default_label": policy.default_label,
            "label_basis": policy.label_basis,
        },
        "evidence_ledger": ledger,
        "analyst_evidence_pack": pack.model_dump(),
    }, separators=(",", ":"))


def run_staged(ctx: RunContext, llm: LLMClient) -> ProcurementDecision:
    policy = base_policy(ctx)
    messages = evidence_messages(ctx, ANALYST_SYSTEM, f"Prepare the evidence pack for purchase request {ctx.request_id}.")
    pack = agent_turns(ctx, llm, messages, EvidencePack, EvidencePack.model_json_schema(), ANALYST_FINAL)
    ctx.memo["evidence_pack"] = pack.model_dump()

    reviewer_messages = [
        {"role": "system", "content": REVIEWER_SYSTEM},
        {"role": "user", "content": _reviewer_input(ctx, pack)},
    ]
    assessment = structured_call(ctx, llm, reviewer_messages, REVIEWER_FINAL.format(allowed=policy.allowed_labels),
                                 AgentAssessment, assessment_json_schema(), "reviewer_assessment")

    # Union, not override: the reviewer cannot drop the analyst's upward findings.
    assessment.additional_data_classes = sorted(set(assessment.additional_data_classes) | set(pack.additional_data_classes))
    known = {m.field for m in assessment.additional_missing_information}
    assessment.additional_missing_information += [m for m in pack.additional_missing_information if m.field not in known]
    assessment.injection_suspected = assessment.injection_suspected or pack.injection_suspected
    if not assessment.next_step_detail and pack.clarification_questions:
        assessment.next_step_detail = "Questions for the requester: " + " ".join(pack.clarification_questions[:3])
    ctx.memo["assessment"] = assessment.model_dump()
    return build_decision(ctx, policy, assessment)
