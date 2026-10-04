"""Shared agent mechanics. Hand-rolled (no framework) so every LLM and tool call is
counted and the loop has hard limits.

Evidence-first design: the mandatory evidence tools are executed through the tool
registry before the first LLM call and handed to the agent as tool results. The
agent keeps native function calling for FOLLOW-UP evidence. Measured reason (pilot,
gpt-oss-120b on Groq): letting the model call the five tools itself took 6 LLM
calls / ~10k tokens per request (one tool per turn) and it skipped a mandatory
tool, which exceeds a free-tier daily token budget for a full evaluation.
"""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src.llm.client import LLMClient, LLMError
from src.runtime import RunContext
from src.tools import MANDATORY_TOOLS, run_tool, tool_specs

FOLLOW_UP_TOOLS = ["search_existing_software", "get_vendor_profile"]
MAX_FOLLOW_UP_TURNS = 2
T = TypeVar("T", bound=BaseModel)


def inline_refs(schema: dict) -> dict:
    """Inline $ref/$defs and drop titles/defaults (smaller, and some endpoints reject refs)."""
    schema = dict(schema)
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k not in {"title", "default", "description"}}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def evidence_messages(ctx: RunContext, system: str, user: str, tools: list[str] | None = None) -> list[dict]:
    """System + user prompt followed by the mandatory tool calls and their results."""
    names = tools or MANDATORY_TOOLS
    calls = [{"id": f"pre_{i}", "type": "function", "function": {"name": n, "arguments": "{}"}} for i, n in enumerate(names)]
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
        {"role": "assistant", "content": "", "tool_calls": calls},
    ]
    for call in calls:
        output = run_tool(ctx, call["function"]["name"], {})
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output, separators=(",", ":"))})
    return messages


def _extract_json(text: str | None) -> str:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start:end + 1] if start != -1 and end > start else raw


def agent_turns(ctx: RunContext, llm: LLMClient, messages: list[dict], model: type[T], schema: dict,
                instruction: str) -> T:
    """Agent may call follow-up tools; its final answer is JSON validated against `model`."""
    specs = [s for s in tool_specs() if s["function"]["name"] in FOLLOW_UP_TOOLS]
    tag = ctx.memo.get("cache_tag", "")
    messages = messages + [{"role": "user", "content": instruction + "\nJSON schema:\n"
                            + json.dumps(inline_refs(schema), separators=(",", ":"))}]
    reply: dict = {}
    for turn in range(MAX_FOLLOW_UP_TURNS + 1):
        reply = llm.chat(ctx, messages, tools=specs if turn < MAX_FOLLOW_UP_TURNS else None, cache_tag=tag)
        if not reply["tool_calls"]:
            break
        messages.append({
            "role": "assistant", "content": reply["content"] or "",
            "tool_calls": [{"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t["arguments"] or "{}"}}
                           for t in reply["tool_calls"]],
        })
        for call in reply["tool_calls"]:
            try:
                args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            output = run_tool(ctx, call["name"], args if isinstance(args, dict) else {}) if call["name"] in FOLLOW_UP_TOOLS \
                else {"status": "error", "error": f"tool '{call['name']}' is not available"}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output, separators=(",", ":"))})
    try:
        return model.model_validate_json(_extract_json(reply.get("content")))
    except (ValidationError, ValueError) as exc:
        ctx.log("structured_output_invalid", error=str(exc)[:300])
        messages = messages + [{"role": "assistant", "content": reply.get("content") or ""}]
        return structured_call(ctx, llm, messages, f"That was not valid JSON for the schema: {str(exc)[:400]}. "
                               "Return only the corrected JSON.", model, schema, "repair")


def structured_call(ctx: RunContext, llm: LLMClient, messages: list[dict], instruction: str,
                    model: type[T], schema: dict, name: str) -> T:
    """JSON-mode call (no tools); raises LLMError if the output still does not validate."""
    tag = ctx.memo.get("cache_tag", "")
    response_format = {"type": "json_schema", "json_schema": {"name": name, "schema": inline_refs(schema)}}
    reply = llm.chat(ctx, messages + [{"role": "user", "content": instruction}], response_format=response_format, cache_tag=tag)
    try:
        return model.model_validate_json(_extract_json(reply["content"]))
    except (ValidationError, ValueError) as exc:
        raise LLMError(f"invalid structured output: {str(exc)[:200]}") from exc
