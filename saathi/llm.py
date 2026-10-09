"""Strict-JSON LLM calls for every agent (intake, extraction, precedents, drafter, critic).

All calls use the Gemini API through the google-genai SDK's models.generate_content (structured JSON output):

    client = genai.Client(api_key=GEMINI_API_KEY)
    client.models.generate_content(model=GEMINI_MODEL, contents=prompt,
                                   config={"system_instruction": system, "response_mime_type": "application/json",
                                           "response_json_schema": schema, "temperature": 0.1,
                                           "thinking_config": {"thinking_level": "low"}}).text

LLM_API=interactions switches to the Interactions API (client.interactions.create), measured ~3x slower per call.

Without GEMINI_API_KEY (or with LLM_PROVIDER=none) json_call returns None and every agent runs its deterministic
rule-based fallback. Responses are cached on disk.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import time
import types
from functools import lru_cache

from config import CACHE_DIR, GEMINI_API_KEY, GEMINI_MODEL, LLM_API, LLM_PROVIDER
from saathi import timing

LLM_CACHE = CACHE_DIR / "llm"
LLM_CACHE.mkdir(parents=True, exist_ok=True)
ATTEMPTS = 2  # 1 retry
THINKING_LEVEL = "low"  # every Gemini call, every agent - one setting, no per-call overrides
# JSON-Schema keywords pydantic emits that Gemini's response schema doesn't need / may reject.
_DROP = {"$defs", "$schema", "title", "default", "additionalProperties", "examples"}


def enabled() -> bool:
    return LLM_PROVIDER != "none" and bool(GEMINI_API_KEY)


def label() -> str:
    if not enabled():
        return "rule-based (no Gemini key)"
    try:
        return f"Gemini ({GEMINI_MODEL}, {api()})"
    except Exception:
        return f"Gemini ({GEMINI_MODEL})"


@lru_cache(maxsize=1)
def client():
    from google import genai

    return genai.Client(api_key=GEMINI_API_KEY)


def gemini_schema(schema: dict) -> dict:
    """Inline $ref/$defs (pydantic nests models) and drop keywords Gemini's response schema doesn't use."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            if "allOf" in node and len(node["allOf"]) == 1:  # pydantic wraps a lone $ref in allOf
                return walk({**node["allOf"][0], **{k: v for k, v in node.items() if k != "allOf"}})
            return {k: walk(v) for k, v in node.items() if k not in _DROP}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


def _parse(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)  # tolerate fenced output
    return json.loads(m.group(1) if m else text)


def api() -> str:
    """'generate_content' by default: measured ~1.2-2s per call vs ~4.5s through the Interactions API for the same
    model and prompt (eval/latency.py). LLM_API=interactions opts back in (google-genai >= 2.x only)."""
    if LLM_API == "interactions" and hasattr(client(), "interactions"):
        return "interactions"
    return "generate_content"


def _create(system: str, prompt: str, schema: dict | None):
    if api() == "interactions":
        fmt = {"type": "text", "mime_type": "application/json"}
        if schema is not None:
            fmt["schema"] = schema
        return client().interactions.create(
            model=GEMINI_MODEL,
            input=prompt,
            system_instruction=system,
            response_format=fmt,
            generation_config={"temperature": 0.1, "thinking_level": THINKING_LEVEL},
        )
    config = {"system_instruction": system, "response_mime_type": "application/json", "temperature": 0.1,
              "thinking_config": {"thinking_level": THINKING_LEVEL}}
    if schema is not None:
        config["response_json_schema"] = schema
    resp = client().models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
    return types.SimpleNamespace(status="completed", output_text=resp.text)


def call_gemini(system: str, prompt: str, schema: dict) -> dict:
    """One structured call with a retry. If Gemini rejects the schema itself, retry in plain JSON mode with the
    schema spelled out in the instruction."""
    g_schema = gemini_schema(schema)
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            interaction = _create(system, prompt, g_schema)
            if getattr(interaction, "status", "completed") not in ("completed", None):
                raise RuntimeError(f"interaction status {interaction.status}")
            out = _parse(interaction.output_text)
            if not isinstance(out, dict):
                raise ValueError("model did not return a JSON object")
            missing = [k for k in schema.get("required", []) if k not in out]
            if missing:
                raise ValueError(f"JSON missing required keys: {missing}")
            return out
        except Exception as e:  # API error, bad JSON or missing keys -> retry once
            last = e
            if g_schema is not None and re.search(r"schema|response_format|invalid argument", str(e), re.I):
                g_schema = None
                system = f"{system}\n\nReturn ONLY a JSON object matching this JSON Schema:\n{json.dumps(schema)}"
            if attempt < ATTEMPTS:
                time.sleep(1.5)
    raise last or RuntimeError("Gemini call failed")


def json_call(system: str, prompt: str, schema: dict) -> dict | None:
    """A dict matching `schema`, or None when Gemini isn't configured or the call fails (callers fall back)."""
    if not enabled():
        return None
    key = hashlib.sha1(json.dumps([GEMINI_MODEL, THINKING_LEVEL, system, prompt, schema], sort_keys=True).encode()).hexdigest()
    path = LLM_CACHE / f"{key}.json"
    if path.exists():
        try:
            with timing.span("llm", f"{system[:60]} · {len(prompt)} chars in", cached=True):
                return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    try:
        with timing.span("llm", f"{system[:60]} · {len(prompt)} chars in"):
            out = call_gemini(system, prompt, schema)
    except Exception as e:  # never fail a complaint on an LLM error; the agent's rule-based path takes over
        print(f"  [llm] {GEMINI_MODEL} failed after {ATTEMPTS} attempts: {type(e).__name__}: {str(e)[:200]}",
              file=sys.stderr)
        return None
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
