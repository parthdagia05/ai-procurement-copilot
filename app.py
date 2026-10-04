from __future__ import annotations

import streamlit as st

from src import data_access
from src.decision import LABEL_TEXT, label_of
from src.llm.client import LLMClient
from src.solution import run

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")

LABEL_STYLE = {
    "proceed_to_standard_approval": ("green", "Proceed to standard approval"),
    "route_for_specialist_review": ("orange", "Route for specialist review"),
    "use_existing_tool": ("blue", "Use existing tool"),
    "request_clarification": ("violet", "Request clarification"),
    "hold_for_manual_review": ("red", "Hold for manual review"),
}
SOURCE_TITLES = {
    "get_request_context": "Request & requester",
    "check_budget": "Budget",
    "search_existing_software": "Existing tools & purchases",
    "get_vendor_profile": "Vendor registry & risk service",
    "evaluate_policy_rules": "Policy rules (deterministic)",
    "copilot_analysis": "Copilot interpretation (LLM, cites evidence ids)",
}


def evidence_icon(reference: str, finding: str) -> str:
    text = finding.lower()
    if "unavailable" in text or "could not be verified" in text or "cannot be verified" in text:
        return "✕"
    if "#untrusted-text" in reference or "reconciliation" in reference or "expired" in text or "disagree" in text \
            or "missing" in text or "exceeds" in text or "unknown" in text:
        return "⚠"
    return "✓"


requests = data_access.load_requests()
by_id = {r["request_id"]: r for r in requests}
llm = LLMClient()

with st.sidebar:
    st.header("Request queue")
    request_id = st.selectbox("Request", list(by_id), format_func=lambda rid: f"{rid} · {by_id[rid]['product_name']}")
    architecture = st.radio("Architecture", ["single", "staged"], horizontal=True,
                            format_func=lambda a: "A · single agent" if a == "single" else "B · analyst → reviewer")
    if llm.available:
        st.caption(f"LLM: `{llm.settings.model}` via {llm.settings.provider}")
    else:
        st.warning("No LLM key configured: running the deterministic policy engine only.")
    analyse = st.button("Run analysis", type="primary", use_container_width=True)

st.title("Procurement Request Copilot")
st.info("Recommendations are advisory. A human makes every approval; the copilot never purchases, approves or changes budgets.",
        icon="🛡️")

key = f"{request_id}:{architecture}"
if analyse:
    with st.spinner("Gathering evidence and applying policy…"):
        try:
            st.session_state[key] = run(request_id, architecture)
        except Exception as exc:  # surface, don't crash the page
            st.session_state[key] = exc
result = st.session_state.get(key)

req = by_id[request_id]
left, middle, right = st.columns([0.9, 1.25, 1.1], gap="large")

with left:
    st.subheader("Request details")
    rows = {
        "Product": req.get("product_name"), "Vendor": req.get("vendor_name"), "Category": req.get("category"),
        "Annual cost (USD)": req.get("annual_cost_usd"), "Users": req.get("user_count"),
        "Data access": req.get("data_access_level"),
        "Integrations": ", ".join(req.get("requested_integrations") or []) or "none",
        "Urgency": req.get("urgency"), "Requester": req.get("requester_id"),
    }
    st.table({"Field": list(rows), "Value": [("— missing —" if v in (None, "", "unknown") else str(v)) for v in rows.values()]})
    st.markdown("**Business justification** *(untrusted text, shown as data)*")
    ctx = result[1] if isinstance(result, tuple) else None
    rc = ctx.memo.get("request_context") if ctx else None
    if rc and rc.injection_hits:
        st.error(f"⚠ Instruction-like content detected and ignored:\n\n> {req.get('business_justification')}")
    else:
        st.code(req.get("business_justification") or "— missing —", language=None, wrap_lines=True)

if result is None:
    with middle:
        st.subheader("Evidence")
        st.caption("Run the analysis to gather evidence.")
elif isinstance(result, Exception):
    with middle:
        st.exception(result)
else:
    decision, ctx = result
    label = label_of(decision)
    with middle:
        st.subheader("Evidence")
        groups: dict[str, list] = {}
        for item in decision.evidence:
            groups.setdefault(item.source, []).append(item)
        for source, items in groups.items():
            with st.expander(f"{SOURCE_TITLES.get(source, source)} ({len(items)})", expanded=source != "evaluate_policy_rules"):
                for item in items:
                    st.markdown(f"{evidence_icon(item.reference or '', item.finding)} {item.finding}  \n`{item.reference}`")

    with right:
        st.subheader("Recommendation")
        color, title = LABEL_STYLE.get(label, ("gray", label))
        st.markdown(f"### :{color}[{title}]")
        st.write(decision.recommendation.split(":", 1)[1].strip().removeprefix(LABEL_TEXT.get(label, "")).lstrip(". "))

        policy = ctx.memo.get("final_policy")
        reasons = {a.role: a.reasons for a in policy.approvals} if policy else {}
        st.markdown("**Approvals required** (human sign-off)")
        for role in decision.required_approvals:
            st.checkbox(role, value=False, disabled=True, key=f"{key}:{role}",
                        help="; ".join(reasons.get(role, [])) or None)
        if decision.risk_flags:
            st.markdown("**Risk flags**  \n" + " ".join(f"`{f}`" for f in decision.risk_flags))
        if decision.missing_information:
            st.markdown("**Missing information**")
            for m in decision.missing_information:
                st.markdown(f"- {m}")
        st.markdown("**Next step**")
        st.write(decision.next_step)

        st.markdown("**Reviewer action**")
        a1, a2, a3 = st.columns(3)
        if a1.button("Send to approvers", disabled=label in {"request_clarification", "hold_for_manual_review"}):
            st.success(f"Routed to {', '.join(decision.required_approvals)} (simulated). Nothing was purchased.")
        if a2.button("Request clarification"):
            asks = decision.missing_information or ["confirm the business need and whether existing tools could cover it"]
            st.info("Draft to requester:\n\n" + "\n".join(f"- {m}" for m in asks))
        if a3.button("Escalate to manual review"):
            st.warning("Escalated to Procurement for manual review (simulated).")

    t = decision.telemetry
    st.divider()
    net = (t.latency_ms or 0) - (t.rate_limit_wait_ms or 0)
    cols = st.columns(5)
    cols[0].metric("Latency (excl. rate-limit wait)", f"{net / 1000:.1f}s")
    cols[1].metric("LLM calls", t.llm_calls)
    cols[2].metric("Tool calls", t.tool_calls)
    cols[3].metric("Model", t.model or "none")
    cols[4].metric("Mode", "fallback" if t.fallback_reason else "LLM + policy floor")
    if t.fallback_reason:
        st.caption(f"Deterministic fallback: {t.fallback_reason}")
    with st.expander("Run trace"):
        st.json(ctx.trace)
