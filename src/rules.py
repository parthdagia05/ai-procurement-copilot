"""Deterministic policy engine (data/procurement_policy.md as code).

Produces the decision FLOOR: approvals, hard risk flags, missing information and
the set of recommendation labels the LLM may choose from. The LLM can add to the
floor (src/decision.py) but never remove from it. Every approval and flag
carries a policy-section citation.

Ambiguity rulings (see docs/DESIGN_PROPOSAL.md s1.2):
  P1 bands are <=1,000 / <=10,000 / <=25,000 / >25,000; Legal new-vendor spend is >= 10,000
  P2 a review is current while days_since_review <= 365
  P4/P5 terms are standard if "approved" or "standard"; sensitive data + outside region -> Privacy + Legal
  P6 SSO alone is not employee PII
  P7 unknown data level -> Security + Privacy (never assumed safe)
  P8 unknown cost -> no guessed tier; Procurement triages
"""
from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel

from src.facts import (
    BudgetCheck, RequestContext, SoftwareSearch, VendorProfile, check_budget, request_context,
    search_existing_software, usd, vendor_profile,
)
from src.runtime import RunContext

ROLES = ["Manager", "Department Head", "Procurement", "Finance", "CFO", "Security", "Privacy", "Legal"]
SPECIALIST_ROLES = {"Security", "Privacy", "Legal"}
LABELS = [
    "proceed_to_standard_approval", "route_for_specialist_review", "use_existing_tool",
    "request_clarification", "hold_for_manual_review",
]
FLAGS = [
    "existing_tool_overlap", "budget_insufficient", "budget_unverifiable", "security_review_required",
    "privacy_review_required", "legal_review_required", "vendor_review_expired", "conflicting_vendor_evidence",
    "vendor_risk_unavailable", "vendor_assessment_missing", "prompt_injection_detected", "missing_information",
]

# Policy thresholds (s4, s7). tests/test_rules.py checks these against the policy text.
BAND_1_MAX = Decimal("1000")
BAND_2_MAX = Decimal("10000")
BAND_3_MAX = Decimal("25000")
LEGAL_NEW_VENDOR_MIN = Decimal("10000")

SECURITY_DATA = {"source_code", "production_access", "confidential_documents", "employee_pii", "customer_pii", "credentials"}
PII = {"employee_pii", "customer_pii"}
REGION_SENSITIVE = {"employee_pii", "customer_pii", "confidential_documents", "credentials"}

# Known data-access levels. Anything else goes to keyword classification below.
DATA_LEVELS: dict[str, set[str]] = {
    "none": set(), "public": set(), "internal": set(), "internal_only": set(),
    "internal_documents": set(),  # internal != confidential (REQ-1001 must not trigger Security)
    "internal_marketing": set(), "marketing": set(),
    "source_code": {"source_code"},
    "production_telemetry": {"production_access"}, "production": {"production_access"},
    "production_data": {"production_access"}, "cloud_account": {"production_access"},
    "confidential_documents": {"confidential_documents"}, "confidential": {"confidential_documents"},
    "employee_pii": {"employee_pii"}, "customer_pii": {"customer_pii"}, "pii": {"employee_pii", "customer_pii"},
    "credentials": {"credentials"}, "secrets": {"credentials"},
}
# Keyword fallbacks for unseen data levels and integration names: (regexes, data classes).
# Word-boundary regexes, so "Document repository" is not source code and "digital" is not git.
KEYWORD_RULES: list[tuple[re.Pattern[str], set[str]]] = [
    (re.compile(p, re.IGNORECASE), classes)
    for p, classes in [
        (r"\b(customers?|clients?|crm|salesforce|hubspot|helpdesk\w*|zendesk|tickets?|ticketing|support desk)\b", {"customer_pii"}),
        (r"\b(employees?|hris|workday|bamboohr|payroll|hr|personnel|staff records)\b", {"employee_pii"}),
        (r"\b(pii|personal data|personal)\b", {"employee_pii", "customer_pii"}),
        (r"\b(source code|source_code|git|github|gitlab|bitbucket|code repos?\w*|codebase)\b", {"source_code"}),
        (r"\b(production|prod|cloud accounts?|aws|gcp|azure|kubernetes|k8s|infrastructure|databases?)\b", {"production_access"}),
        (r"\b(confidential|restricted|contracts?|document repos?\w*|sharepoint|dropbox|google drive|file shares?)\b", {"confidential_documents"}),
        (r"\b(credentials?|secrets?|passwords?|vault|key management)\b", {"credentials"}),
    ]
]
IDENTITY_ONLY = re.compile(r"\b(sso|okta|saml|single sign-on|entra)\b", re.IGNORECASE)  # ruling P6


class Approval(BaseModel):
    role: str
    reasons: list[str]


class Flag(BaseModel):
    flag: str
    reasons: list[str]


class PolicyResult(BaseModel):
    approvals: list[Approval]
    flags: list[Flag]
    missing: list[str]
    allowed_labels: list[str]
    default_label: str
    label_basis: str
    data_classes: list[str]
    data_level_unknown: bool
    unclassified_inputs: list[str]
    financial_band: str | None
    overlap_candidates: list[str]

    def roles(self) -> list[str]:
        return [a.role for a in self.approvals]

    def flag_names(self) -> list[str]:
        return [f.flag for f in self.flags]


def _keyword_classes(text: str) -> set[str]:
    text = text.replace("_", " ")
    found: set[str] = set()
    for pattern, classes in KEYWORD_RULES:
        if pattern.search(text):
            found |= classes
    return found


def classify_data(level: object, integrations: list | None) -> tuple[set[str], bool, list[str], list[str]]:
    """-> (data classes, level unknown?, explanations, unclassified inputs)."""
    classes: set[str] = set()
    notes: list[str] = []
    unclassified: list[str] = []
    raw = str(level or "").strip().lower().replace(" ", "_").replace("-", "_")
    unknown = raw in {"", "unknown", "tbd", "tbc", "n/a", "na", "unspecified", "null", "?"}
    if not unknown:
        if raw in DATA_LEVELS:
            classes |= DATA_LEVELS[raw]
        else:
            guessed = _keyword_classes(raw)
            if guessed:
                classes |= guessed
                notes.append(f"data level '{level}' classified by keyword as {', '.join(sorted(guessed))}")
            else:
                unknown = True  # unrecognised vocabulary is not assumed safe (ruling P7)
                unclassified.append(f"data_access_level: {level}")
    for item in integrations or []:
        text = str(item).strip()
        if IDENTITY_ONLY.search(text):
            continue
        guessed = _keyword_classes(text)
        if guessed:
            classes |= guessed
            notes.append(f"integration '{item}' implies {', '.join(sorted(guessed))}")
        else:
            unclassified.append(f"integration: {item}")
    return classes, unknown, notes, unclassified


def financial_band(cost: Decimal) -> tuple[str, list[str]]:
    if cost <= BAND_1_MAX:
        return "up to $1,000", ["Manager"]
    if cost <= BAND_2_MAX:
        return "$1,000.01-$10,000", ["Department Head", "Procurement"]
    if cost <= BAND_3_MAX:
        return "$10,000.01-$25,000", ["Department Head", "Finance", "Procurement"]
    return "above $25,000", ["Department Head", "Finance", "CFO", "Procurement"]


class _Builder:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.approvals: dict[str, list[str]] = {}
        self.flags: dict[str, list[str]] = {}

    def approve(self, role: str, reason: str) -> None:
        self.approvals.setdefault(role, [])
        if reason not in self.approvals[role]:
            self.approvals[role].append(reason)

    def flag(self, name: str, reason: str) -> None:
        self.flags.setdefault(name, [])
        if reason not in self.flags[name]:
            self.flags[name].append(reason)

    def cite(self, ref: str, finding: str) -> None:
        self.ctx.add_evidence(ref, "evaluate_policy_rules", finding)


def evaluate_policy(ctx: RunContext, extra_data_classes: set[str] | None = None) -> PolicyResult:
    """Pure function of the run's facts. extra_data_classes lets the LLM raise (never lower) sensitivity."""
    rc: RequestContext = request_context(ctx)
    budget: BudgetCheck = check_budget(ctx)
    software: SoftwareSearch = search_existing_software(ctx)
    vendor: VendorProfile = vendor_profile(ctx, rc.request.get("vendor_name") or "")
    req = rc.request
    b = _Builder(ctx)

    # s1 required information
    missing = list(rc.missing_fields)
    if missing:
        b.flag("missing_information", "Required request fields missing: " + "; ".join(missing))

    # s4 financial approvals
    cost_present = next(f.present for f in rc.completeness if f.field == "annual_cost_usd")
    band = None
    if cost_present:
        cost = Decimal(str(req["annual_cost_usd"]))
        band, roles = financial_band(cost)
        for role in roles:
            b.approve(role, f"s4: annual amount {usd(req['annual_cost_usd'])} is in band {band}")
        b.cite("policy:s4", f"Annual amount {usd(req['annual_cost_usd'])} falls in the {band} band: minimum approvals {', '.join(roles)} (policy s4).")
    else:
        b.approve("Procurement", "s4: annual cost unknown, approval tier cannot be determined; Procurement triages")
        b.cite("policy:s4", "Annual cost is missing, so the financial approval tier cannot be determined; no tier is guessed (policy s1, s4).")

    # s2 budget
    if budget.status == "exceeds":
        b.flag("budget_insufficient", f"request {usd(budget.request_cost_usd)} exceeds available {usd(budget.available_usd)}")
        b.approve("Finance", "s2: cost exceeds available budget; budget exception review")
        b.cite("policy:s2", f"Cost exceeds {budget.department}'s available budget: Finance budget-exception review required (policy s2).")
    elif budget.status == "unverifiable_no_budget_record":
        b.flag("budget_unverifiable", f"no budget record for department '{budget.department}'")
        b.approve("Finance", "s2/s10: department budget cannot be verified")
        missing.append(f"department budget record ({budget.department or 'unknown department'})")
        b.cite("policy:s2", "Budget could not be verified: not assumed sufficient; routed to Finance (policy s2, s10).")

    # s3 overlap (advisory; never an automatic rejection)
    overlap = [c for c in software.candidates if "same_category" in c.match_types]
    if overlap:
        names = ", ".join(f"{c.product_name} ({c.software_id})" for c in overlap)
        b.flag("existing_tool_overlap", f"same-category catalog products: {names}")
        b.cite("policy:s3", f"Existing catalog products in the same category ({names}) must be considered before a new purchase; overlap is not an automatic rejection (policy s3).")

    # s5/s6/s7 data sensitivity
    classes, level_unknown, notes, unclassified = classify_data(req.get("data_access_level"), req.get("requested_integrations"))
    classes |= set(extra_data_classes or ()) & (SECURITY_DATA | PII)
    sec_data = sorted(classes & SECURITY_DATA)
    if sec_data:
        b.approve("Security", f"s5: data/integration involves {', '.join(sec_data)}")
        b.cite("policy:s5#data", f"Security review required because the request involves {', '.join(sec_data)}"
               + (f" ({'; '.join(notes)})" if notes else "") + " (policy s5).")
    if level_unknown:
        b.approve("Security", "s5/s10: data access level unknown; sensitivity cannot be ruled out")
        b.approve("Privacy", "s6/s10: data access level unknown; personal data cannot be ruled out")
        b.cite("policy:s10#data-unknown", "Intended data access level is unknown or unrecognised: Security and Privacy review until clarified; no favourable status assumed (policy s5, s6, s10).")

    # s5 vendor assessment: any source not current, missing, unverifiable or disputed -> Security
    vendor_reasons = []
    if vendor.api_status == "unavailable":
        b.flag("vendor_risk_unavailable", f"vendor-risk service {vendor.api_reason}")
        vendor_reasons.append("vendor-risk service unavailable, assessment unverified")
    if vendor.api_status == "not_found":
        b.flag("vendor_assessment_missing", "vendor-risk service has no assessment record")
        vendor_reasons.append("no vendor security assessment on record")
    if vendor.conflict:
        b.flag("conflicting_vendor_evidence", "; ".join(vendor.conflict_details))
        vendor_reasons.append("registry and vendor-risk service disagree")
    if vendor.expired:
        b.flag("vendor_review_expired", "security review older than 365 days")
        vendor_reasons.append("vendor security review expired")
    statuses = {a.effective_status for a in (vendor.registry_assessment, vendor.api_assessment) if a}
    if statuses & {"not_completed", "rejected"}:
        vendor_reasons.append("vendor security assessment not completed")
    if "unknown" in statuses:
        vendor_reasons.append("vendor security status unknown")
    if vendor_reasons or not vendor.assessment_current:
        reason = "; ".join(vendor_reasons) or "vendor security assessment is not current"
        b.approve("Security", f"s5: {reason}")
        b.cite("policy:s5#vendor", f"Security review required: {reason} (policy s5).")

    # s6 privacy and s7 cross-region
    pii = sorted(classes & PII)
    if pii:
        b.approve("Privacy", f"s6: tool will process {', '.join(pii)}")
        b.cite("policy:s6", f"Privacy review required: the tool will process {', '.join(pii)} (policy s6).")
    region_sensitive = classes & REGION_SENSITIVE or level_unknown
    if region_sensitive and vendor.stores_data_outside_region:
        b.approve("Privacy", "s6: sensitive data may be stored outside the operating region")
        b.approve("Legal", "s7: material cross-region data-processing issue")
        b.cite("policy:s6-s7#region", "Vendor stores data outside the operating region and the request involves sensitive data: Privacy and Legal review (policy s6, s7).")
    elif region_sensitive and vendor.stores_data_outside_region is None:
        b.approve("Privacy", "s6/s10: data region unverified for sensitive data")
        b.cite("policy:s6#region-unverified", "Data residency could not be verified for sensitive data: Privacy review; no favourable status assumed (policy s6, s10).")

    # s7 legal
    if vendor.is_new_vendor and cost_present and Decimal(str(req["annual_cost_usd"])) >= LEGAL_NEW_VENDOR_MIN:
        b.approve("Legal", f"s7: new vendor with annual spend {usd(req['annual_cost_usd'])} (>= $10,000)")
    if vendor.is_new_vendor and not cost_present:
        b.approve("Legal", "s7: new vendor and annual spend unknown; $10,000 threshold cannot be ruled out")
    if not vendor.legal_terms_standard:
        b.approve("Legal", f"s7: legal terms are '{vendor.legal_terms_status or 'not on record'}', not approved/standard")
    if "Legal" in b.approvals:
        b.cite("policy:s7", "Legal review required: " + "; ".join(r.removeprefix("s7: ") for r in b.approvals["Legal"] if r.startswith("s7")) + " (policy s7).")

    # s8 AI tools: informational; the approval effect comes from s5/s6.
    if "ai" in str(req.get("category") or "").lower().split() or " ai" in f" {str(req.get('product_name') or '').lower()}":
        b.cite("policy:s8", "AI tool: prior approvals of this vendor do not extend to new use cases or data classes (policy s8).")

    # s9 injection
    hits = rc.injection_hits + software.injection_hits + vendor.injection_hits
    if hits:
        b.flag("prompt_injection_detected", "; ".join(sorted({f"{h.location} ({h.pattern})" for h in hits})))

    # Specialist flags mirror specialist approvals.
    for role, flag in (("Security", "security_review_required"), ("Privacy", "privacy_review_required"), ("Legal", "legal_review_required")):
        if role in b.approvals:
            b.flag(flag, "; ".join(b.approvals[role]))

    # Recommendation labels the LLM may choose from (precedence order).
    evidence_gap = vendor.api_status == "unavailable" or vendor.conflict or budget.status == "unverifiable_no_budget_record"
    specialist = bool(SPECIALIST_ROLES & set(b.approvals)) or budget.status == "exceeds"
    can_reuse = bool(overlap) or any(c.available_to_department for c in software.candidates if "same_vendor" in c.match_types)
    if rc.missing_fields:
        allowed, default, basis = ["request_clarification"], "request_clarification", "required request information is missing"
    elif evidence_gap:
        allowed, default, basis = ["hold_for_manual_review"], "hold_for_manual_review", "material evidence is unavailable, conflicting or unverifiable"
    elif specialist:
        allowed, default, basis = ["route_for_specialist_review"], "route_for_specialist_review", "specialist review or budget exception required"
    else:
        allowed, default, basis = ["proceed_to_standard_approval"], "proceed_to_standard_approval", "no specialist trigger"
    if can_reuse and not rc.missing_fields:
        allowed.append("use_existing_tool")

    approvals = [Approval(role=r, reasons=b.approvals[r]) for r in ROLES if r in b.approvals]
    flags = [Flag(flag=f, reasons=b.flags[f]) for f in FLAGS if f in b.flags]
    return PolicyResult(
        approvals=approvals, flags=flags, missing=missing, allowed_labels=allowed, default_label=default,
        label_basis=basis, data_classes=sorted(classes), data_level_unknown=level_unknown,
        unclassified_inputs=unclassified, financial_band=band,
        overlap_candidates=[c.software_id for c in software.candidates],
    )
