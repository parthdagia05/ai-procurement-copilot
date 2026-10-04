"""Builds the final ProcurementDecision from the deterministic floor plus (optionally)
the LLM's assessment. This is the ONLY place a decision is assembled.

Guarantees (asserted at runtime, tested in tests/test_decision.py):
  - every code-derived approval and risk flag is present in the output;
  - the LLM can only ADD approvals / flags / data classes / missing fields;
  - the recommendation label is one the policy engine allowed;
  - LLM interpretations must cite evidence ids that exist in this run's ledger;
  - model text claiming an approval was granted is discarded;
  - human_review_required is always True (the copilot never approves).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.contracts import EvidenceItem, ProcurementDecision
from src.facts import REQUIRED_FIELDS, request_context, search_existing_software
from src.rules import FLAGS, LABELS, ROLES, SPECIALIST_ROLES, Approval, Flag, PolicyResult, evaluate_policy
from src.runtime import RunContext
from src.safety import claims_approval_granted

LABEL_TEXT = {
    "proceed_to_standard_approval": "Proceed to standard approval",
    "route_for_specialist_review": "Route for specialist review before approval",
    "use_existing_tool": "Use the existing tool instead of a new purchase",
    "request_clarification": "Request clarification from the requester",
    "hold_for_manual_review": "Hold for manual review",
}
FIELD_LABELS = dict(REQUIRED_FIELDS)
ROLE_FLAG = {"Security": "security_review_required", "Privacy": "privacy_review_required", "Legal": "legal_review_required"}


# ------------------------------------------------------------------ LLM output contract

class AddedApproval(BaseModel):
    role: str
    reason: str


class AddedFlag(BaseModel):
    flag: str
    reason: str


class AddedMissing(BaseModel):
    field: str = Field(description="One of: " + ", ".join(FIELD_LABELS))
    question: str


class Interpretation(BaseModel):
    finding: str
    evidence_ids: list[str]


class AgentAssessment(BaseModel):
    recommendation_label: str
    rationale: str
    next_step_detail: str = ""
    overlap_assessment: str = ""
    additional_approvals: list[AddedApproval] = Field(default_factory=list)
    additional_risk_flags: list[AddedFlag] = Field(default_factory=list)
    additional_data_classes: list[str] = Field(default_factory=list)
    additional_missing_information: list[AddedMissing] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    interpretations: list[Interpretation] = Field(default_factory=list)
    injection_suspected: bool = False


def assessment_json_schema() -> dict:
    """Strict-enough JSON schema for response_format (enums constrain the model)."""
    schema = AgentAssessment.model_json_schema()
    schema["properties"]["recommendation_label"]["enum"] = LABELS
    defs = schema.get("$defs", {})
    defs["AddedApproval"]["properties"]["role"]["enum"] = ROLES
    defs["AddedFlag"]["properties"]["flag"]["enum"] = FLAGS
    defs["AddedMissing"]["properties"]["field"]["enum"] = list(FIELD_LABELS)
    return schema


# ------------------------------------------------------------------ deterministic text

def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else "none"


def _summary(label: str, policy: PolicyResult, ctx: RunContext) -> str:
    roles = policy.roles()
    specialists = [r for r in roles if r in SPECIALIST_ROLES]
    if label == "request_clarification":
        return "Missing: " + "; ".join(policy.missing) + "."
    if label == "hold_for_manual_review":
        return f"{policy.label_basis.capitalize()}; {_join(specialists or ['Procurement'])} must review before approval routing."
    if label == "route_for_specialist_review":
        extra = " plus a Finance budget exception" if "budget_insufficient" in policy.flag_names() else ""
        return f"{_join(specialists) if specialists else 'Finance'} review required{extra} before {_join([r for r in roles if r not in SPECIALIST_ROLES])} approval."
    if label == "use_existing_tool":
        cands = search_existing_software(ctx).candidates
        names = [f"{c.product_name} ({c.scope}, {c.licensed_seats} seats)" for c in cands if c.available_to_department] or [c.product_name for c in cands]
        return f"Already licensed: {_join(names[:2])}."
    return f"Annual amount in the {policy.financial_band} band; approvals: {_join(roles)}."


def _next_step(label: str, policy: PolicyResult, ctx: RunContext) -> str:
    roles = policy.roles()
    specialists = [r for r in roles if r in SPECIALIST_ROLES]
    business = [r for r in roles if r not in SPECIALIST_ROLES]
    if label == "request_clarification":
        return f"Ask the requester to provide: {'; '.join(policy.missing)}. Re-run the review once answered; nothing is routed for approval until then."
    if label == "hold_for_manual_review":
        return (f"Hold the request. {_join(specialists or ['Procurement'])} to manually verify the unresolved evidence "
                f"({policy.label_basis}) before routing to {_join(business)} for approval.")
    if label == "route_for_specialist_review":
        return (f"Send the evidence package to {_join(specialists or ['Finance'])} for review, then to {_join(business)} for approval. "
                "Do not purchase until every approval is recorded.")
    if label == "use_existing_tool":
        return (f"Ask the requester whether the existing tool meets the need. If a genuine gap remains, route to {_join(roles)} "
                "with the gap documented.")
    return f"Send to {_join(roles)} for approval. No specialist review was triggered."


# ------------------------------------------------------------------ build

def build_decision(ctx: RunContext, base: PolicyResult, assessment: AgentAssessment | None = None) -> ProcurementDecision:
    policy = base
    notes: dict[str, int] = {"ungrounded_dropped": 0, "unsafe_text_dropped": 0, "label_rejected": 0}
    rationale, detail = "", ""
    extra_evidence: list[EvidenceItem] = []

    if assessment is not None:
        # 1. Upward-only data classification re-runs the engine; more classes can only add approvals.
        if assessment.additional_data_classes:
            policy = evaluate_policy(ctx, set(assessment.additional_data_classes))
        approvals = {a.role: list(a.reasons) for a in policy.approvals}
        flags = {f.flag: list(f.reasons) for f in policy.flags}
        missing = list(policy.missing)

        for add in assessment.additional_approvals:
            if add.role in ROLES:
                approvals.setdefault(add.role, []).append(f"copilot: {add.reason}")
        for add in assessment.additional_risk_flags:
            if add.flag in FLAGS:
                flags.setdefault(add.flag, []).append(f"copilot: {add.reason}")
        if assessment.injection_suspected:
            flags.setdefault("prompt_injection_detected", []).append("copilot: instruction-like content in business data")
        for add in assessment.additional_missing_information:
            label = FIELD_LABELS.get(add.field)
            if label and label not in missing:
                missing.append(label)
        if len(missing) > len(policy.missing):
            flags.setdefault("missing_information", []).append("copilot: request needs clarification")
        for role, flag in ROLE_FLAG.items():
            if role in approvals:
                flags.setdefault(flag, []).append(f"{role} review required")

        # LLM-identified gaps in the request force clarification, exactly like code-found gaps.
        clarify = len([m for m in missing if m in FIELD_LABELS.values()]) > 0
        policy = policy.model_copy(update={
            "approvals": [Approval(role=r, reasons=approvals[r]) for r in ROLES if r in approvals],
            "flags": [Flag(flag=f, reasons=flags[f]) for f in FLAGS if f in flags],
            "missing": missing,
            "allowed_labels": ["request_clarification"] if clarify else list(policy.allowed_labels),
            "default_label": "request_clarification" if clarify else policy.default_label,
        })

        label = assessment.recommendation_label
        if label not in policy.allowed_labels:
            notes["label_rejected"] += 1
            label = policy.default_label

        for text_name in ("rationale", "next_step_detail"):
            text = getattr(assessment, text_name).strip()
            if claims_approval_granted(text):
                notes["unsafe_text_dropped"] += 1
                text = ""
            if text_name == "rationale":
                rationale = text
            else:
                detail = text
        for item in assessment.interpretations:
            valid = [i for i in item.evidence_ids if i in ctx.ledger]
            if not valid or claims_approval_granted(item.finding):
                notes["ungrounded_dropped" if not valid else "unsafe_text_dropped"] += 1
                continue
            extra_evidence.append(EvidenceItem(source="copilot_analysis", finding=item.finding.strip(), reference=", ".join(valid)))
        if assessment.overlap_assessment.strip() and not claims_approval_granted(assessment.overlap_assessment):
            overlap_refs = [k for k in ctx.ledger if k.startswith(("catalog:", "purchase:"))]
            if overlap_refs:
                extra_evidence.append(EvidenceItem(source="copilot_analysis", finding=assessment.overlap_assessment.strip(),
                                                   reference=", ".join(overlap_refs[:4])))
    else:
        label = policy.default_label

    ctx.memo["merge_notes"] = notes
    ctx.log("merge", label=label, **notes)

    recommendation = f"{label}: {LABEL_TEXT[label]}. {rationale or _summary(label, policy, ctx)}"
    next_step = _next_step(label, policy, ctx) + (f" {detail}" if detail else "")
    decision = ProcurementDecision(
        request_id=ctx.request_id,
        recommendation=recommendation,
        evidence=list(ctx.ledger.values()) + extra_evidence,
        required_approvals=policy.roles(),
        missing_information=list(policy.missing),
        risk_flags=policy.flag_names(),
        next_step=next_step,
        human_review_required=True,
        telemetry=ctx.telemetry.to_contract(ctx.elapsed_ms()),
    )
    # The floor is never lowered, whatever the model said.
    assert set(base.roles()) <= set(decision.required_approvals), "code-derived approval dropped"
    assert set(base.flag_names()) <= set(decision.risk_flags), "code-derived risk flag dropped"
    assert decision.human_review_required is True
    return decision


def label_of(decision: ProcurementDecision) -> str:
    return decision.recommendation.split(":", 1)[0].strip()


def build_deterministic(ctx: RunContext) -> ProcurementDecision:
    from src.tools import MANDATORY_TOOLS, base_policy, run_tool

    for name in MANDATORY_TOOLS:
        run_tool(ctx, name, {})
    return build_decision(ctx, base_policy(ctx))
