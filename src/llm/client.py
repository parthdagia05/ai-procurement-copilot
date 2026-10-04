"""OpenAI-compatible chat client with:

- a process-wide limiter for requests/minute and tokens/minute (free tiers are tight),
- 429 handling that honours the server's retry hint and fails fast on daily quotas,
- a hash-keyed response cache with three modes (LLM_CACHE env var):
    off     - always call the API (default for the app)
    record  - reuse a cached response if present, otherwise call and store it
    replay  - cache only; a miss raises CacheMiss (offline, reproducible evals)
- telemetry on the RunContext (LLM calls, rate-limit wait).

Cached files hold model outputs only (no keys, no headers); tests scan them for secrets.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

from src.config import ROOT
from src.llm.settings import LLMSettings, load_settings
from src.runtime import RunContext

CACHE_DIR = ROOT / "evals" / "llm_cache"


class LLMError(RuntimeError):
    """Any failure that should send the run to the deterministic fallback."""


class CacheMiss(LLMError):
    pass


class QuotaExhausted(LLMError):
    pass


class _Limiter:
    """Sliding 60-second window over requests and (estimated) tokens."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: deque[tuple[float, int]] = deque()

    def wait(self, rpm: int, tpm: int, tokens: int) -> float:
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] >= 60:
                    self.events.popleft()
                used = sum(t for _, t in self.events)
                if len(self.events) < rpm and (used + tokens <= tpm or not self.events):
                    self.events.append((now, tokens))
                    return waited
                pause = max(60 - (now - self.events[0][0]), 0.05)
            time.sleep(min(pause, 2.0))
            waited += min(pause, 2.0)

    def correct(self, estimated: int, actual: int) -> None:
        with self.lock:
            for i in range(len(self.events) - 1, -1, -1):
                ts, tok = self.events[i]
                if tok == estimated:
                    self.events[i] = (ts, actual)
                    return


_LIMITER = _Limiter()
_RETRY_IN = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s", re.IGNORECASE)


def _estimate_tokens(payload: dict) -> int:
    return int(len(json.dumps(payload)) / 3.8) + 500  # ~3.8 chars/token measured on these prompts + output allowance


class LLMClient:
    def __init__(self, settings: LLMSettings | None = None, cache_mode: str | None = None):
        self.settings = settings or load_settings()
        self.cache_mode = (cache_mode or os.getenv("LLM_CACHE", "off")).lower()
        self.tpm = int(os.getenv("LLM_TPM", "8000"))
        self._client = OpenAI(api_key=self.settings.api_key or "replay-only", base_url=self.settings.base_url,
                              max_retries=0, timeout=60)

    @property
    def available(self) -> bool:
        return self.settings.enabled or self.cache_mode == "replay"

    def _extra(self) -> dict:
        model = self.settings.model
        effort = os.getenv("LLM_REASONING_EFFORT")
        if effort is None:
            effort = "low" if "gpt-oss" in model else ("none" if "qwen" in model else "")
        return {"reasoning_effort": effort} if effort else {}

    def chat(self, ctx: RunContext, messages: list[dict], *, tools: list[dict] | None = None,
             response_format: dict | None = None, cache_tag: str = "") -> dict:
        payload: dict[str, Any] = {"model": self.settings.model, "messages": messages, "temperature": 0, **self._extra()}
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        key = hashlib.sha256(json.dumps({"p": payload, "tag": cache_tag}, sort_keys=True).encode()).hexdigest()[:32]
        path = CACHE_DIR / self.settings.model.replace("/", "__") / f"{key}.json"
        ctx.telemetry.model = self.settings.model

        if self.cache_mode in {"record", "replay"} and path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            ctx.telemetry.llm_calls += 1
            ctx.log("llm", cached=True, key=key, tool_calls=[t["name"] for t in cached["tool_calls"]], usage=cached.get("usage"))
            return cached
        if self.cache_mode == "replay":
            raise CacheMiss(f"no cached response for {key}")
        if not self.settings.enabled:
            raise LLMError("no LLM API key configured")

        estimate = _estimate_tokens(payload)
        start = time.perf_counter()
        for attempt in range(1, 7):
            ctx.telemetry.rate_limit_wait_ms += _LIMITER.wait(self.settings.rpm, self.tpm, estimate) * 1000
            try:
                resp = self._client.chat.completions.create(**payload)
                break
            except RateLimitError as exc:
                text = str(exc)
                if "per day" in text.lower() or "(tpd)" in text.lower() or "(rpd)" in text.lower():
                    raise QuotaExhausted(text[:300]) from exc
                match = _RETRY_IN.search(text)
                pause = (int(match.group(1) or 0) * 60 + float(match.group(2))) if match else 2.0 * attempt
                ctx.telemetry.rate_limit_wait_ms += (pause + 0.5) * 1000
                ctx.log("llm_rate_limited", attempt=attempt, pause_s=pause)
                time.sleep(pause + 0.5)
            except (APIConnectionError, APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                if status is not None and status < 500 and status != 408:
                    raise LLMError(f"{type(exc).__name__} {status}: {str(exc)[:300]}") from exc
                if attempt >= 3:
                    raise LLMError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
                time.sleep(1.5 * attempt)
        else:
            raise LLMError("rate limited: retries exhausted")

        msg = resp.choices[0].message
        usage = resp.usage.model_dump(include={"prompt_tokens", "completion_tokens", "total_tokens"}) if resp.usage else {}
        _LIMITER.correct(estimate, usage.get("total_tokens", estimate))
        result = {
            "content": msg.content,
            "tool_calls": [{"id": t.id, "name": t.function.name, "arguments": t.function.arguments} for t in (msg.tool_calls or [])],
            "usage": usage,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
        }
        ctx.telemetry.llm_calls += 1
        ctx.log("llm", cached=False, key=key, tool_calls=[t["name"] for t in result["tool_calls"]], usage=usage,
                latency_ms=result["latency_ms"])
        if self.cache_mode == "record":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result
