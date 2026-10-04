"""Deterministic evidence gathering. Every function:

- takes keys (request id / vendor name), never values paraphrased by an LLM,
- is memoised on the RunContext (each source is read once per run),
- writes human-readable findings into the evidence ledger with a stable reference,
- treats every free-text field as untrusted and scans it for injection.

Nothing here makes a policy decision; src/rules.py does that.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from src import data_access
from src.config import reference_date
from src.runtime import RunContext
from src.safety import InjectionHit, scan
from src.vendor_client import fetch_vendor_risk

REVIEW_VALIDITY_DAYS = 365  # policy s5: current for 365 days from the review date
LEADERSHIP_LEVELS = {"director", "vp", "head", "svp", "evp", "c-level", "chief"}
MISSING_TOKENS = {"", "unknown", "tbd", "tbc", "n/a", "na", "?", "unspecified", "not sure", "null", "none provided", "-"}

# Policy s1 required fields -> label used in missing_information.
REQUIRED_FIELDS = [
    ("requester", "requester and department"),
    ("product", "product/vendor"),
    ("annual_cost_usd", "annual cost (or a reasonable annual estimate)"),
    ("user_count", "number of users/licenses"),
    ("business_justification", "business purpose"),
    ("data_access_level", "intended data access level"),
    ("requested_integrations", "required integrations"),
]


def usd(amount: float | int | None) -> str:
    if amount is None:
        return "unknown"
    return f"${amount:,.2f}" if float(amount) != int(amount) else f"${int(amount):,}"


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def is_missing_text(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in MISSING_TOKENS)


def _memo(ctx: RunContext, key: str, build):
    if key not in ctx.memo:
        ctx.memo[key] = build()
    return ctx.memo[key]


# --------------------------------------------------------------------------- request

class FieldStatus(BaseModel):
    field: str
    label: str
    present: bool


class RequestContext(BaseModel):
    request_id: str
    request: dict[str, Any]
    requester: dict | None
    department: str | None
    manager: dict | None
    department_head: dict | None
    requester_is_department_head: bool
    completeness: list[FieldStatus]
    missing_fields: list[str]
    injection_hits: list[InjectionHit]


def _department_head(employees: list[dict], employee: dict) -> dict | None:
    dept = employee.get("department")
    leaders = [e for e in employees if e.get("department") == dept and _norm(e.get("level")) in LEADERSHIP_LEVELS]
    if leaders:
        return leaders[0]
    by_id = {e["employee_id"]: e for e in employees}
    current, seen = employee, set()
    while current and current.get("manager_id") and current["employee_id"] not in seen:
        seen.add(current["employee_id"])
        current = by_id.get(current["manager_id"])
        if current and _norm(current.get("level")) in LEADERSHIP_LEVELS:
            return current
    return None


def request_context(ctx: RunContext) -> RequestContext:
    return _memo(ctx, "request_context", lambda: _build_request_context(ctx))


def _build_request_context(ctx: RunContext) -> RequestContext:
    req = data_access.get_request(ctx.request_id)
    employees = data_access.employees()
    by_id = {e["employee_id"]: e for e in employees}
    requester = by_id.get(req.get("requester_id"))
    department = requester.get("department") if requester else None
    manager = by_id.get(requester.get("manager_id")) if requester and requester.get("manager_id") else None
    head = _department_head(employees, requester) if requester else None

    cost, users = req.get("annual_cost_usd"), req.get("user_count")
    present = {
        "requester": requester is not None and not is_missing_text(department),
        "product": not is_missing_text(req.get("product_name")) and not is_missing_text(req.get("vendor_name")),
        "annual_cost_usd": isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0,
        "user_count": isinstance(users, int) and not isinstance(users, bool) and users > 0,
        "business_justification": not is_missing_text(req.get("business_justification")),
        "data_access_level": not is_missing_text(req.get("data_access_level")),
        # [] means "no integrations"; only an absent/null value is missing.
        "requested_integrations": isinstance(req.get("requested_integrations"), list),
    }
    completeness = [FieldStatus(field=f, label=label, present=present[f]) for f, label in REQUIRED_FIELDS]
    missing = [fs.label for fs in completeness if not fs.present]

    hits: list[InjectionHit] = []
    for key in ["product_name", "vendor_name", "category", "business_justification", "data_access_level", "urgency"]:
        hits += scan(f"request.{key}", req.get(key))
    for item in req.get("requested_integrations") or []:
        hits += scan("request.requested_integrations", item)

    integrations = ", ".join(req.get("requested_integrations") or []) or "none"
    ctx.add_evidence(
        f"request:{ctx.request_id}", "get_request_context",
        f"{req.get('product_name')} from {req.get('vendor_name')} ({req.get('category')}): annual cost {usd(cost)}, "
        f"{users if users is not None else 'unknown'} users, data access '{req.get('data_access_level')}', "
        f"integrations: {integrations}, urgency '{req.get('urgency')}'.",
    )
    if requester:
        mgr = f"{manager['name']} ({manager['employee_id']})" if manager else "none on record"
        note = " Requester is the department's own leader; approval must go to the next level." if head and head["employee_id"] == requester["employee_id"] else ""
        ctx.add_evidence(
            f"employee:{requester['employee_id']}", "get_request_context",
            f"Requester {requester['name']} ({requester['level']}, {department}); manager {mgr}.{note}",
        )
    if missing:
        ctx.add_evidence(
            f"request:{ctx.request_id}#completeness", "get_request_context",
            "Required request information missing (policy s1): " + "; ".join(missing) + ".",
        )
    if hits:
        ctx.add_evidence(
            f"request:{ctx.request_id}#untrusted-text", "get_request_context",
            "Request text contains instructions aimed at the reviewer; treated as untrusted data and ignored (policy s9): "
            + " | ".join(f'"{h.excerpt}"' for h in hits[:2]),
        )
    return RequestContext(
        request_id=ctx.request_id, request=req, requester=requester, department=department, manager=manager,
        department_head=head, requester_is_department_head=bool(head and requester and head["employee_id"] == requester["employee_id"]),
        completeness=completeness, missing_fields=missing, injection_hits=hits,
    )


# --------------------------------------------------------------------------- budget

class BudgetCheck(BaseModel):
    department: str | None
    status: str  # within | exceeds | unverifiable_no_budget_record | unverifiable_no_cost
    annual_budget_usd: float | None = None
    committed_usd: float | None = None
    available_usd: float | None = None
    request_cost_usd: float | None = None
    headroom_usd: float | None = None


def check_budget(ctx: RunContext) -> BudgetCheck:
    return _memo(ctx, "check_budget", lambda: _build_budget(ctx))


def _build_budget(ctx: RunContext) -> BudgetCheck:
    rc = request_context(ctx)
    dept = rc.department
    row = next((b for b in data_access.budgets() if dept and _norm(b["department"]) == _norm(dept)), None)
    cost_present = next(f.present for f in rc.completeness if f.field == "annual_cost_usd")
    cost = rc.request.get("annual_cost_usd") if cost_present else None
    if row is None:
        ctx.add_evidence(
            f"budget:{dept or 'unknown-department'}", "check_budget",
            f"No software budget record for department '{dept or 'unknown'}'; budget cannot be verified (policy s2, s10).",
        )
        return BudgetCheck(department=dept, status="unverifiable_no_budget_record", request_cost_usd=cost)
    available = row["available_usd"]
    base = BudgetCheck(
        department=dept, status="unverifiable_no_cost", annual_budget_usd=row["annual_software_budget_usd"],
        committed_usd=row["committed_usd"], available_usd=available, request_cost_usd=cost,
    )
    summary = (f"{dept} software budget: annual {usd(row['annual_software_budget_usd'])}, committed {usd(row['committed_usd'])}, "
               f"available {usd(available)}")
    if cost is None:
        ctx.add_evidence(f"budget:{dept}", "check_budget", f"{summary}; request cost unknown, so the budget check cannot be completed.")
        return base
    base.headroom_usd = available - cost
    base.status = "within" if cost <= available else "exceeds"
    verdict = "within" if base.status == "within" else "EXCEEDS"
    ctx.add_evidence(
        f"budget:{dept}", "check_budget",
        f"{summary}; request {usd(cost)} is {verdict} the available budget (headroom {usd(base.headroom_usd)}).",
    )
    return base


# --------------------------------------------------------------------------- existing software

class SoftwareCandidate(BaseModel):
    software_id: str
    product_name: str
    category: str | None
    vendor_name: str | None
    status: str | None
    licensed_seats: int | None
    scope: str | None
    annual_cost_usd: float | None
    notes: str | None
    match_types: list[str]
    available_to_department: bool


class SoftwareSearch(BaseModel):
    query: str | None
    candidates: list[SoftwareCandidate]
    purchase_history: list[dict]
    same_category_overlap: bool
    injection_hits: list[InjectionHit]


def search_existing_software(ctx: RunContext, query: str | None = None) -> SoftwareSearch:
    key = f"search_existing_software:{_norm(query)}"
    return _memo(ctx, key, lambda: _build_search(ctx, query))


def _tokens(text: str | None) -> set[str]:
    return {t for t in "".join(c if c.isalnum() else " " for c in _norm(text)).split() if len(t) >= 4}


def _build_search(ctx: RunContext, query: str | None) -> SoftwareSearch:
    rc = request_context(ctx)
    req = rc.request
    req_vendor, req_category = _norm(req.get("vendor_name")), _norm(req.get("category"))
    req_product_tokens = _tokens(req.get("product_name"))
    query_tokens = _tokens(query)
    dept = _norm(rc.department)

    candidates: list[SoftwareCandidate] = []
    hits: list[InjectionHit] = []
    for row in data_access.software_catalog():
        matches = []
        if req_vendor and _norm(row.get("vendor_name")) == req_vendor:
            matches.append("same_vendor")
        if req_category and _norm(row.get("category")) == req_category:
            matches.append("same_category")
        if req_product_tokens & _tokens(row.get("product_name")):
            matches.append("product_name")
        haystack = _tokens(f"{row.get('product_name')} {row.get('category')} {row.get('notes')}")
        if query_tokens and query_tokens & haystack:
            matches.append("keyword")
        if not matches:
            continue
        scope = row.get("scope")
        candidate = SoftwareCandidate(
            software_id=row["software_id"], product_name=row["product_name"], category=row.get("category"),
            vendor_name=row.get("vendor_name"), status=row.get("status"), licensed_seats=row.get("licensed_seats"),
            scope=scope, annual_cost_usd=row.get("annual_cost_usd"), notes=row.get("notes"), match_types=matches,
            available_to_department=_norm(scope) in {"company-wide", "company wide", "all"} or _norm(scope) == dept,
        )
        candidates.append(candidate)
        hits += scan(f"catalog.{row['software_id']}.notes", row.get("notes"))
        restricted = "" if _norm(row.get("status")) == "approved" else " Status is restricted: existing approval may not cover new data classes (policy s8)."
        ctx.add_evidence(
            f"catalog:{row['software_id']}", "search_existing_software",
            f"Existing {row.get('status')} tool {row['product_name']} ({row.get('category')}, vendor {row.get('vendor_name')}): "
            f"{row.get('licensed_seats')} seats, scope {scope}, {usd(row.get('annual_cost_usd'))}/yr - \"{row.get('notes')}\". "
            f"Match: {', '.join(matches)}; {'available' if candidate.available_to_department else 'not licensed'} for {rc.department}.{restricted}",
        )

    history = [p for p in data_access.purchase_history() if req_vendor and _norm(p.get("vendor_name")) == req_vendor]
    for p in history:
        hits += scan(f"purchase_history.{p['purchase_id']}.notes", p.get("notes"))
        ctx.add_evidence(
            f"purchase:{p['purchase_id']}", "search_existing_software",
            f"Prior purchase {p['purchase_id']} on {p['purchase_date']}: {p['department']} bought {p['product_name']} "
            f"for {usd(p.get('annual_amount_usd'))}/yr ({p['status']}; {p.get('notes')}).",
        )
    if not candidates:
        ctx.add_evidence(
            f"catalog:search:{_norm(query) or 'default'}", "search_existing_software",
            f"No approved catalog product shares the vendor, category or product name of {req.get('product_name')}.",
        )
    return SoftwareSearch(
        query=query, candidates=candidates, purchase_history=history,
        same_category_overlap=any("same_category" in c.match_types for c in candidates), injection_hits=hits,
    )


# --------------------------------------------------------------------------- vendor

class SourceAssessment(BaseModel):
    stated_status: str  # approved | not_completed | expired | rejected | unknown
    review_date: str | None
    days_since_review: int | None
    effective_status: str  # current | expired | not_completed | rejected | unknown | missing | unavailable


class VendorProfile(BaseModel):
    vendor_name: str
    registry: dict | None
    api_status: str  # ok | not_found | unavailable
    api_reason: str | None
    api_record: dict | None
    registry_assessment: SourceAssessment | None
    api_assessment: SourceAssessment
    conflict: bool
    conflict_details: list[str]
    expired: bool
    assessment_current: bool
    is_new_vendor: bool
    legal_terms_status: str | None
    legal_terms_standard: bool
    processes_personal_data: bool | None
    stores_data_outside_region: bool | None
    injection_hits: list[InjectionHit]


_STATUS_MAP = {
    "approved": "approved", "current": "approved", "passed": "approved",
    "pending": "not_completed", "not_completed": "not_completed", "in_progress": "not_completed",
    "not completed": "not_completed", "in progress": "not_completed", "not_started": "not_completed",
    "expired": "expired", "rejected": "rejected", "failed": "rejected", "denied": "rejected",
}


def _stated(value: object) -> str:
    return _STATUS_MAP.get(_norm(value), "unknown")


def _assess(stated: str, review_date: str | None, ref: date) -> SourceAssessment:
    days = (ref - date.fromisoformat(review_date)).days if review_date else None
    if stated == "approved":
        effective = "unknown" if days is None else ("current" if days <= REVIEW_VALIDITY_DAYS else "expired")
    else:
        effective = stated
    return SourceAssessment(stated_status=stated, review_date=review_date, days_since_review=days, effective_status=effective)


def vendor_profile(ctx: RunContext, vendor_name: str) -> VendorProfile:
    return _memo(ctx, f"vendor_profile:{_norm(vendor_name)}", lambda: _build_vendor(ctx, vendor_name))


def _build_vendor(ctx: RunContext, vendor_name: str) -> VendorProfile:
    ref = reference_date()
    registry = next((v for v in data_access.vendors() if _norm(v["vendor_name"]) == _norm(vendor_name)), None)
    api = fetch_vendor_risk(vendor_name)
    ctx.log("vendor_api", vendor=vendor_name, status=api.status, reason=api.reason, attempts=api.attempts, latency_ms=api.latency_ms)
    record = {k: v for k, v in (api.data or {}).items() if k != "vendor_name"} if api.status == "ok" else None

    reg_assess = _assess(_stated(registry.get("security_status")), registry.get("security_review_date"), ref) if registry else None
    if api.status == "ok":
        api_assess = _assess(_stated(record.get("security_review_status")), record.get("last_review_date"), ref)
    else:
        api_assess = SourceAssessment(stated_status="unknown", review_date=None, days_since_review=None,
                                      effective_status="missing" if api.status == "not_found" else "unavailable")

    conflict_details: list[str] = []
    if reg_assess and api.status == "ok":
        if "unknown" not in (reg_assess.stated_status, api_assess.stated_status) and reg_assess.stated_status != api_assess.stated_status:
            conflict_details.append(
                f"registry security status '{registry.get('security_status')}' vs vendor-risk service '{record.get('security_review_status')}'")
        if reg_assess.review_date and api_assess.review_date and reg_assess.review_date != api_assess.review_date:
            conflict_details.append(f"registry review date {reg_assess.review_date} vs vendor-risk service {api_assess.review_date}")

    sources = [a for a in (reg_assess, api_assess) if a is not None]
    expired = any(a.effective_status == "expired" for a in sources)
    current = all(a.effective_status == "current" for a in sources) and not conflict_details
    is_new = registry is None or not _norm(registry.get("procurement_status")).startswith("approved")
    terms = registry.get("legal_terms_status") if registry else None
    terms_standard = _norm(terms).startswith(("approved", "standard"))

    hits: list[InjectionHit] = []
    if registry:
        hits += scan("vendor_registry.notes", registry.get("notes"))
    if record:
        hits += scan("vendor_api.notes", record.get("notes"))

    if registry:
        review = registry.get("security_review_date") or "no review date"
        ctx.add_evidence(
            f"vendor_registry:{registry['vendor_id']}", "get_vendor_profile",
            f"Internal registry: {registry['vendor_name']} procurement status {registry.get('procurement_status')}, "
            f"security {registry.get('security_status')} ({review}), legal terms {terms}.",
        )
    else:
        ctx.add_evidence(f"vendor_registry:{vendor_name}", "get_vendor_profile",
                         f"{vendor_name} is not in the internal vendor registry: treated as a new vendor with no approved terms.")
    endpoint = f"vendor_api:{vendor_name}"
    if api.status == "ok":
        ctx.add_evidence(
            endpoint, "get_vendor_profile",
            f"Vendor-risk service: security review {record.get('security_review_status')} "
            f"(last review {record.get('last_review_date') or 'none'}), risk {record.get('risk_level')}, "
            f"processes personal data: {'yes' if record.get('processes_personal_data') else 'no'}, "
            f"stores data outside region: {'yes' if record.get('stores_data_outside_region') else 'no'}.",
        )
    elif api.status == "not_found":
        ctx.add_evidence(endpoint, "get_vendor_profile",
                         f"Vendor-risk service has no assessment record for {vendor_name}: security assessment is missing (policy s5).")
    else:
        ctx.add_evidence(
            endpoint, "get_vendor_profile",
            f"Vendor-risk service unavailable ({api.reason} after {api.attempts} attempt(s)): security status, personal-data "
            f"processing and data region could not be verified; no favourable status assumed (policy s10).",
        )
    for label, assess in (("registry", reg_assess), ("vendor-risk service", api_assess)):
        if assess and assess.days_since_review is not None and assess.effective_status in {"current", "expired"}:
            ctx.add_evidence(
                f"{'vendor_registry' if label == 'registry' else 'vendor_api'}:{vendor_name}#review-age", "get_vendor_profile",
                f"Security review dated {assess.review_date} is {assess.days_since_review} days before the reference date {ref} "
                f"(valid {REVIEW_VALIDITY_DAYS} days): {assess.effective_status} per {label}.",
            )
    if conflict_details:
        ctx.add_evidence(f"vendor_reconciliation:{vendor_name}", "get_vendor_profile",
                         "Sources disagree, not silently resolved (policy s5): " + "; ".join(conflict_details) + ".")
    if hits:
        ctx.add_evidence(
            f"vendor_api:{vendor_name}#untrusted-text", "get_vendor_profile",
            "Vendor text contains instructions aimed at the reviewer; treated as untrusted data and ignored (policy s9): "
            + " | ".join(f'"{h.excerpt}"' for h in hits[:2]),
        )
    return VendorProfile(
        vendor_name=vendor_name, registry=registry, api_status=api.status, api_reason=api.reason, api_record=record,
        registry_assessment=reg_assess, api_assessment=api_assess, conflict=bool(conflict_details),
        conflict_details=conflict_details, expired=expired, assessment_current=current, is_new_vendor=is_new,
        legal_terms_status=terms, legal_terms_standard=terms_standard,
        processes_personal_data=record.get("processes_personal_data") if record else None,
        stores_data_outside_region=record.get("stores_data_outside_region") if record else None,
        injection_hits=hits,
    )
