"""Per-request run state: telemetry, evidence ledger, memoised facts, trace.

One RunContext is created per handle_request call and passed explicitly; there
is no global state, so concurrent UI sessions and eval runs cannot interfere.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.contracts import EvidenceItem, RunTelemetry


@dataclass
class Telemetry:
    llm_calls: int = 0
    tool_calls: int = 0
    tool_names: list[str] = field(default_factory=list)
    rate_limit_wait_ms: float = 0.0
    fallback_reason: str | None = None
    model: str | None = None

    def to_contract(self, latency_ms: float) -> RunTelemetry:
        return RunTelemetry(
            llm_calls=self.llm_calls,
            tool_calls=self.tool_calls,
            tool_names=list(self.tool_names),
            latency_ms=round(latency_ms, 1),
            rate_limit_wait_ms=round(self.rate_limit_wait_ms, 1),
            fallback_reason=self.fallback_reason,
            model=self.model,
        )


@dataclass
class RunContext:
    request_id: str
    architecture: str
    telemetry: Telemetry = field(default_factory=Telemetry)
    # Evidence ledger: stable reference id -> evidence item. Final evidence is built
    # from here, so every item is traceable to a tool result in this run.
    ledger: dict[str, EvidenceItem] = field(default_factory=dict)
    memo: dict[str, Any] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)

    def add_evidence(self, ref: str, source: str, finding: str) -> str:
        if ref not in self.ledger:
            self.ledger[ref] = EvidenceItem(source=source, finding=finding, reference=ref)
        return ref

    def log(self, kind: str, **data: Any) -> None:
        self.trace.append({"t_ms": round((time.perf_counter() - self.started) * 1000, 1), "kind": kind, **data})

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000
