"""Prompt-injection detection over untrusted business data, and output sanitising.

Detection is a recall aid, not the defence. The defence is architectural: the
LLM's output can never remove a code-derived approval or flag (src/decision.py),
and its tools only read data. Patterns target imperative or authority claims,
not the bare word "approved" (e.g. "the approved coding assistant" is benign).
"""
from __future__ import annotations

import re

from pydantic import BaseModel

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in [
        ("ignore_rules", r"\b(ignore|disregard|forget)\b.{0,40}\b(rules?|instructions?|polic(y|ies)|controls?|guidelines?|previous|above)\b"),
        ("bypass_controls", r"\b(bypass|skip|override|circumvent|waive)\b.{0,40}\b(rules?|polic(y|ies)|controls?|reviews?|approvals?|checks?|security|privacy|legal|procurement)\b"),
        ("treat_as_approved", r"\btreat\b.{0,60}\bas\b.{0,20}\b(pre-?)?approved\b"),
        ("authority_approval_claim", r"\b(cfo|ceo|cto|vp|security|legal|privacy|finance|procurement|management)[- ](has[- ])?(pre-?)?approved\b"),
        ("pre_approved_claim", r"\bpre-?approved\b"),
        ("approve_now", r"\bapprove\b.{0,25}\b(immediately|now|automatically|without|right away)\b"),
        ("mark_approved", r"\bmark\b.{0,30}\bapproved\b"),
        ("role_override", r"\b(you are now|act as|new instructions|system prompt|system note|developer message|note to (the )?ai|ai reviewer)\b"),
        ("exfiltration", r"\b(reveal|print|show|send|exfiltrate)\b.{0,30}\b(api[- ]?keys?|secrets?|passwords?|system prompt|credentials)\b"),
    ]
]


class InjectionHit(BaseModel):
    location: str  # e.g. "request.business_justification", "vendor_api.notes"
    pattern: str
    excerpt: str


def scan(location: str, text: object) -> list[InjectionHit]:
    if not isinstance(text, str) or not text.strip():
        return []
    hits: list[InjectionHit] = []
    for name, rx in _PATTERNS:
        match = rx.search(text)
        if match:
            start, end = max(match.start() - 20, 0), min(match.end() + 20, len(text))
            hits.append(InjectionHit(location=location, pattern=name, excerpt=text[start:end].strip()))
    return hits


# Phrases in LLM-written text that would claim THIS request/purchase was approved.
# Factual statements about an existing vendor or tool ("SignFlow is approved") are allowed:
# v1 of this pattern blocked those too: 12 drops across the first two evaluation runs, all
# false positives on grounded statements, so it was narrowed.
_SUBJECT = r"(this |the )?(request|purchase|spend|order|purchase order|acquisition|it|this)"
_GRANTED_CLAIM = re.compile(
    rf"\b{_SUBJECT}\s+(is|has been|was|have been|are)\s+(now\s+|already\s+|fully\s+)?(approved|authori[sz]ed|signed off)\b"
    r"|\b(has|have|had)\s+(already\s+)?(approved|authori[sz]ed|signed off)\s+(this|the|it)\b"
    r"|\b(cfo|ceo|security|legal|privacy|finance|procurement)[- ]approved\b"
    r"|\bpre-?approved\b"
    r"|\bapproval (is |has been )?granted\b"
    r"|\b(approve|purchase|buy) (it|this) (now|immediately)\b",
    re.IGNORECASE,
)


def claims_approval_granted(text: str | None) -> bool:
    """True if model-written text asserts an approval has been granted.

    The copilot only recommends; any such claim is unsafe output (policy s11).
    Statements about an existing approved vendor/tool are allowed through the
    narrower pattern set ("is approved" about a vendor is still blocked: the
    sanitiser prefers a false positive to a fabricated approval).
    """
    # Model text that itself reads like an injection ("skip review") is equally unsafe.
    return bool(text and (_GRANTED_CLAIM.search(text) or scan("model_output", text)))
