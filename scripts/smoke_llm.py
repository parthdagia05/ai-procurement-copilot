"""Phase-0 smoke test: is the OpenAI-compatible endpoint reliable enough to build on?

Checks, repeated N times:
  1. tool calling   - model emits well-formed tool calls (and whether it parallelises them)
  2. round trip     - tool results fed back are accepted
  3. structured out - final answer conforms to a JSON schema (validated with Pydantic)

Usage: .venv/bin/python scripts/smoke_llm.py [--runs 3]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from src.llm.settings import load_settings  # noqa: E402

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_budget",
            "description": "Return the available software budget for a department.",
            "parameters": {
                "type": "object",
                "properties": {"department": {"type": "string"}},
                "required": ["department"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vendor_status",
            "description": "Return the security review status for a vendor.",
            "parameters": {
                "type": "object",
                "properties": {"vendor_name": {"type": "string"}},
                "required": ["vendor_name"],
            },
        },
    },
]
FAKE_RESULTS = {
    "check_budget": {"department": "Marketing", "available_usd": 15000},
    "get_vendor_status": {"vendor_name": "BrandBoard", "security_review_status": "not_completed"},
}


class Verdict(BaseModel):
    label: str
    within_budget: bool
    security_review_needed: bool
    cited_tools: list[str]


SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict",
        "schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": ["route_for_specialist_review", "proceed"]},
                "within_budget": {"type": "boolean"},
                "security_review_needed": {"type": "boolean"},
                "cited_tools": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["label", "within_budget", "security_review_needed", "cited_tools"],
        },
    },
}


def one_run(client: OpenAI, model: str) -> dict:
    out = {"tool_calls_ok": False, "parallel": False, "structured_ok": False, "llm_calls": 0}
    messages = [
        {"role": "system", "content": "You are a procurement assistant. Use the tools to gather facts before answering."},
        {"role": "user", "content": "A Marketing employee wants BrandBoard for $12,000/yr. Check the budget and the vendor's security status."},
    ]
    t0 = time.perf_counter()
    r1 = client.chat.completions.create(model=model, messages=messages, tools=TOOLS, temperature=0)
    out["llm_calls"] += 1
    msg = r1.choices[0].message
    calls = msg.tool_calls or []
    names = []
    for c in calls:
        json.loads(c.function.arguments)  # raises on malformed args
        names.append(c.function.name)
    out["tool_calls_ok"] = bool(calls)
    out["parallel"] = len(calls) >= 2
    out["tools_called"] = names

    # Second turn: return results; if the model only called one tool, let it call the other.
    messages.append(msg.model_dump(exclude_none=True))
    for c in calls:
        messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(FAKE_RESULTS[c.function.name])})
    for _ in range(2):
        if set(names) >= set(FAKE_RESULTS):
            break
        r = client.chat.completions.create(model=model, messages=messages, tools=TOOLS, temperature=0)
        out["llm_calls"] += 1
        m = r.choices[0].message
        if not m.tool_calls:
            break
        messages.append(m.model_dump(exclude_none=True))
        for c in m.tool_calls:
            names.append(c.function.name)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(FAKE_RESULTS[c.function.name])})

    messages.append({"role": "user", "content": "Now give your verdict as JSON."})
    r2 = client.chat.completions.create(model=model, messages=messages, response_format=SCHEMA, temperature=0)
    out["llm_calls"] += 1
    raw = r2.choices[0].message.content or ""
    try:
        v = Verdict.model_validate_json(raw)
        out["structured_ok"] = True
        out["verdict"] = v.model_dump()
    except ValidationError as exc:
        out["structured_error"] = str(exc)[:200]
        out["raw"] = raw[:200]
    out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    s = load_settings()
    print(f"provider={s.provider} model={s.model} base_url={s.base_url}")
    if not s.enabled:
        print("NO KEY: set LLM_API_KEY in .env (see .env.example). Skipping.")
        return 2
    client = OpenAI(api_key=s.api_key, base_url=s.base_url, max_retries=2)
    ok = 0
    for i in range(args.runs):
        try:
            res = one_run(client, s.model)
        except Exception as exc:  # report, keep going
            print(f"run {i + 1}: ERROR {type(exc).__name__}: {str(exc)[:300]}")
            continue
        good = res["tool_calls_ok"] and res["structured_ok"]
        ok += good
        print(f"run {i + 1}: {'PASS' if good else 'FAIL'} {json.dumps(res)}")
        time.sleep(60 / max(s.rpm, 1) * res["llm_calls"])  # respect free-tier RPM
    print(f"\n{ok}/{args.runs} runs fully passed")
    return 0 if ok == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
