"""Strict-JSON LLM calls used by the planner, extraction, relevance and drafting agents.

Every call goes to Gemini through a LiteLLM endpoint via the OpenAI client (LITELLM_BASE_URL, LITELLM_API_KEY,
LITELLM_MODEL), in JSON mode, routed to Google AI Studio. Without those settings, or with LLM_PROVIDER=none,
json_call returns None and every agent runs its deterministic rule-based fallback. Responses are cached on disk.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from config import CACHE_DIR, LITELLM_API_KEY, LITELLM_BASE_URL, LITELLM_MODEL, LLM_PROVIDER

LLM_CACHE = CACHE_DIR / "llm"
LLM_CACHE.mkdir(parents=True, exist_ok=True)
ATTEMPTS = 2  # 1 retry


def enabled() -> bool:
    return LLM_PROVIDER != "none" and bool(LITELLM_BASE_URL and LITELLM_API_KEY and LITELLM_MODEL)


def label() -> str:
    return f"Gemini via LiteLLM ({LITELLM_MODEL})" if enabled() else "rule-based (no LLM configured)"


def _prompt(system: str, prompt: str, schema: dict) -> str:
    """JSON mode doesn't enforce a schema, so the schema goes into the (single) user message."""
    return (f"{system}\n\nReturn ONLY a JSON object that matches this JSON Schema (all required keys present):\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n\n---\n\n{prompt}")


def _parse(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)  # tolerate fenced output
    return json.loads(m.group(1) if m else text)


async def ajson_call_uncached(system: str, prompt: str, schema: dict) -> dict:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)
    last_exc: Exception | None = None
    try:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = await client.chat.completions.create(
                    model=LITELLM_MODEL,
                    messages=[{"role": "user", "content": _prompt(system, prompt, schema)}],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                    extra_body={"provider": {"only": ["google-ai-studio"], "allow_fallbacks": True}},
                )
                out = _parse(response.choices[0].message.content)
                if not isinstance(out, dict):
                    raise ValueError("model did not return a JSON object")
                missing = [k for k in schema.get("required", []) if k not in out]
                if missing:
                    raise ValueError(f"JSON missing required keys: {missing}")
                return out
            except Exception as e:  # API error, bad JSON or missing keys -> retry once
                last_exc = e
                if attempt < ATTEMPTS:
                    await asyncio.sleep(1.5)
    finally:
        await client.close()
    raise last_exc or RuntimeError("LLM call failed")


def _run(coro):
    """Run a coroutine from sync code, whether or not an event loop is already running in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def json_call(system: str, prompt: str, schema: dict, *, effort: str = "low") -> dict | None:
    """A dict matching `schema`, or None when no LLM is configured or the call fails (callers fall back).
    `effort` is accepted for call-site compatibility; JSON mode on this endpoint has no effort control."""
    if not enabled():
        return None
    key = hashlib.sha1(json.dumps([LITELLM_MODEL, system, prompt, schema], sort_keys=True).encode()).hexdigest()
    path = LLM_CACHE / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    try:
        out = _run(ajson_call_uncached(system, prompt, schema))
    except Exception as e:  # never fail a case on an LLM error; the agent's rule-based path takes over
        print(f"  [llm] {LITELLM_MODEL} call failed after {ATTEMPTS} attempts: {type(e).__name__}: {str(e)[:200]}",
              file=sys.stderr)
        return None
    path.write_text(json.dumps(out, ensure_ascii=False))
    return out
