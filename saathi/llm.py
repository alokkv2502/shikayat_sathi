"""Strict-JSON LLM calls for every agent (intake, extraction, precedents, drafter, critic).

All calls use the Gemini API through the google-genai SDK's models.generate_content (structured JSON output):

    client = genai.Client(api_key=GEMINI_API_KEY)
    client.models.generate_content(model=GEMINI_MODEL, contents=prompt,
                                   config={"system_instruction": system, "response_mime_type": "application/json",
                                           "response_json_schema": schema, "temperature": 0.1,
                                           "thinking_config": {"thinking_level": "low"}}).text

LLM_API=interactions switches to the Interactions API (client.interactions.create), measured ~3x slower per call.

The agents need the LLM - there is no silent rule-based fallback that would quietly write a worse letter. Without
GEMINI_API_KEY (or with LLM_PROVIDER=none), or when Gemini fails after its retry, json_call raises AIUnavailable;
the node wrapper lets it through, so the UI / CLI say the AI is unavailable instead. Responses are cached on disk.
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
THINKING_LEVEL = "low"  # the default for every call; a call may ask for more (gap_check's questions: "medium")
# JSON-Schema keywords pydantic emits that Gemini's response schema doesn't need / may reject.
_DROP = {"$defs", "$schema", "title", "default", "additionalProperties", "examples"}


class AIUnavailable(RuntimeError):
    """The LLM is not configured, failed after its retry, or returned output that does not match the schema."""


def enabled() -> bool:
    return LLM_PROVIDER != "none" and bool(GEMINI_API_KEY)


def label() -> str:
    if not enabled():
        return "AI unavailable (no Gemini key)"
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


def _create(system: str, prompt: str, schema: dict | None, thinking: str = THINKING_LEVEL):
    if api() == "interactions":
        fmt = {"type": "text", "mime_type": "application/json"}
        if schema is not None:
            fmt["schema"] = schema
        return client().interactions.create(
            model=GEMINI_MODEL,
            input=prompt,
            system_instruction=system,
            response_format=fmt,
            generation_config={"temperature": 0.1, "thinking_level": thinking},
        )
    config = {"system_instruction": system, "response_mime_type": "application/json", "temperature": 0.1,
              "thinking_config": {"thinking_level": thinking}}
    if schema is not None:
        config["response_json_schema"] = schema
    resp = client().models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
    return types.SimpleNamespace(status="completed", output_text=resp.text)


def call_gemini(system: str, prompt: str, schema: dict, thinking: str = THINKING_LEVEL) -> dict:
    """One structured call with a retry. If Gemini rejects the schema itself, retry in plain JSON mode with the
    schema spelled out in the instruction."""
    g_schema = gemini_schema(schema)
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            interaction = _create(system, prompt, g_schema, thinking)
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


def json_call(system: str, prompt: str, schema: dict, thinking: str | None = None) -> dict:
    """A dict matching `schema`. Raises AIUnavailable when Gemini isn't configured or the call fails. `thinking`
    (low | medium | high) overrides THINKING_LEVEL for this call only."""
    thinking = thinking or THINKING_LEVEL
    if not enabled():
        raise AIUnavailable("the AI service is not configured (set GEMINI_API_KEY)")
    key = hashlib.sha1(json.dumps([GEMINI_MODEL, thinking, system, prompt, schema], sort_keys=True).encode()).hexdigest()
    path = LLM_CACHE / f"{key}.json"
    if path.exists():
        try:
            with timing.span("llm", f"{system[:60]} · {len(prompt)} chars in", cached=True):
                return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    try:
        with timing.span("llm", f"{system[:60]} · {len(prompt)} chars in" + (f" · thinking {thinking}" if thinking != THINKING_LEVEL else "")):
            out = call_gemini(system, prompt, schema, thinking)
    except Exception as e:
        print(f"  [llm] {GEMINI_MODEL} failed after {ATTEMPTS} attempts: {type(e).__name__}: {str(e)[:200]}",
              file=sys.stderr)
        raise AIUnavailable(f"the AI service failed ({type(e).__name__}: {str(e)[:120]})") from e
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def validated(model, out: dict):
    """Parse an LLM answer into `model`, or raise AIUnavailable - an answer we can't use is not silently replaced."""
    try:
        return model.model_validate(out)
    except Exception as e:
        raise AIUnavailable(f"the AI returned an unusable {model.__name__} ({type(e).__name__})") from e
