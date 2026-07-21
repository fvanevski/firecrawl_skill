#!/usr/bin/env python3
"""Provider-aware structured-output gateway for Firecrawl workflows.

The gateway deliberately owns transport and response normalization only.  It
does not decide whether research is good, relevant, current, or authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from research_store.semantic_service import redact_sensitive, validate_structured_payload
except ModuleNotFoundError:  # Loaded as scripts.model_gateway from the repository root.
    from scripts.research_store.semantic_service import redact_sensitive, validate_structured_payload


DEFAULT_LOCAL_URL = "http://192.168.4.115:8002/v1"
MAX_RAW_EXCERPT = 4096


def estimate_tokens(value) -> int:
    """Cheap sizing estimate used only for context budgeting."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _redact(value):
    return redact_sensitive(str(value or ""))


def _json_content(raw):
    choices = raw.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return message.get("content"), {
            "finish_reason": choices[0].get("finish_reason"),
            "stop_reason": choices[0].get("stop_reason"),
            "refusal": message.get("refusal"),
            "reasoning_excerpt": _redact(message.get("reasoning", ""))[:MAX_RAW_EXCERPT],
        }
    if raw.get("output_text") is not None:
        return raw.get("output_text"), {"finish_reason": raw.get("status")}
    texts = []
    refusal = None
    for item in raw.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                texts.append(content.get("text", ""))
            if content.get("type") == "refusal":
                refusal = content.get("refusal")
    return "".join(texts), {"finish_reason": raw.get("status"), "refusal": refusal}


def _gemini_content(raw):
    candidates = raw.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
    content = "".join(part.get("text", "") for part in parts)
    return content, {
        "finish_reason": candidates[0].get("finishReason") if candidates else None,
        "block_reason": (raw.get("promptFeedback") or {}).get("blockReason"),
        "safety_ratings": candidates[0].get("safetyRatings", []) if candidates else [],
    }


def schema_errors(value, schema, path="$"):
    """Compatibility wrapper around the shared deterministic validator."""
    return validate_structured_payload(value, schema, path)


def provider_config(provider, model=None):
    if provider == "local":
        return {
            "base_url": os.environ.get("FIRECRAWL_LLM_LOCAL_BASE_URL", os.environ.get("FIRECRAWL_AUDIT_LOCAL_BASE_URL", DEFAULT_LOCAL_URL)).rstrip("/"),
            "model": model or os.environ.get("FIRECRAWL_LLM_LOCAL_MODEL", os.environ.get("FIRECRAWL_AUDIT_LOCAL_MODEL", "chat")),
            "api_key": os.environ.get("FIRECRAWL_LLM_LOCAL_API_KEY", os.environ.get("FIRECRAWL_AUDIT_LOCAL_API_KEY", "")),
            "api_surface": "chat_completions",
        }
    if provider == "openai":
        if not model:
            raise ValueError("commercial OpenAI calls require an explicit model")
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ValueError("OPENAI_API_KEY is required")
        return {"base_url": "https://api.openai.com/v1", "model": model, "api_key": key, "api_surface": "responses"}
    if provider == "gemini":
        if not model:
            raise ValueError("commercial Gemini calls require an explicit model")
        key = os.environ.get("GOOGLE_API_KEY", "")
        if not key:
            raise ValueError("GOOGLE_API_KEY is required")
        return {"base_url": "https://generativelanguage.googleapis.com/v1beta", "model": model, "api_key": key, "api_surface": "generate_content"}
    raise ValueError(f"unsupported LLM provider: {provider}")


def probe_local(base_url, api_key=""):
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with urlopen(Request(base_url + "/models", headers=headers), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("data", [])
        return {
            "status": "available",
            "models": [item.get("id") for item in models if isinstance(item, dict)],
            "max_context_tokens": max((item.get("max_model_len", 0) or 0 for item in models if isinstance(item, dict)), default=0),
        }
    except Exception as exc:
        return {"status": "unavailable", "error": _redact(f"{type(exc).__name__}: {exc}")[:500]}


class StructuredResult:
    def __init__(self, value, provenance, attempts, error="", *, semantic_call_id=None, artifact_ids=()):
        self.value = value
        self.provenance = provenance
        self.attempts = attempts
        self.error = error
        self.semantic_call_id = semantic_call_id
        self.artifact_ids = tuple(artifact_ids)


def _request_json(url, payload, headers, timeout):
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body), response.headers.get("x-request-id"), response.status
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:MAX_RAW_EXCERPT]
        raise RuntimeError(f"HTTP {exc.code}: {_redact(body)}") from exc


def call_structured(provider, model, system_prompt, user_prompt, schema, *,
                    max_output_tokens=16384, timeout=120, max_attempts=3,
                    prompt_version="unversioned", semantic_persistence=None,
                    semantic_context=None):
    system_prompt = _redact(system_prompt)
    user_prompt = _redact(user_prompt)
    config = provider_config(provider, model)
    context = semantic_context or {}
    prompt_hash = hashlib.sha256((system_prompt + "\n" + user_prompt).encode()).hexdigest()
    headers = {"Content-Type": "application/json"}
    if config["api_key"]:
        headers["Authorization"] = f"Bearer {config['api_key']}"
    call_id = None
    if semantic_persistence is not None:
        call_id = semantic_persistence.start_model_call(
            context,
            provider=provider,
            requested_model=config["model"],
            model_revision=str(context.get("model_revision") or ""),
            endpoint_alias="local" if provider == "local" else provider,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema=schema,
            input_token_estimate=estimate_tokens(system_prompt + user_prompt),
        )
    capability = probe_local(config["base_url"], config["api_key"]) if provider == "local" else {"status": "not_probed"}
    attempts = []
    artifacts = []
    output_budget = max_output_tokens
    last_error = ""
    for attempt_number in range(1, max_attempts + 1):
        started = time.monotonic()
        structured_mode = "json_schema" if attempt_number == 1 else "json_object"
        if config["api_surface"] == "responses":
            payload = {
                "model": config["model"], "instructions": system_prompt, "input": user_prompt,
                "max_output_tokens": output_budget,
                "text": {"format": {"type": "json_schema", "name": "firecrawl_result", "strict": True, "schema": schema}},
            }
            url = config["base_url"] + "/responses"
        elif config["api_surface"] == "generate_content":
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": 0, "maxOutputTokens": output_budget,
                    "responseMimeType": "application/json", "responseJsonSchema": schema,
                },
            }
            url = f"{config['base_url']}/models/{quote(config['model'], safe='')}:generateContent?key={quote(config['api_key'], safe='')}"
            headers.pop("Authorization", None)
        else:
            response_format = (
                {"type": "json_schema", "json_schema": {"name": "firecrawl_result", "strict": True, "schema": schema}}
                if structured_mode == "json_schema" else {"type": "json_object"}
            )
            repair = ("\nRepair the prior response. Return only one JSON object matching this exact schema:\n" + json.dumps(schema, sort_keys=True)) if attempt_number > 1 else ""
            payload = {
                "model": config["model"], "temperature": 0, "max_tokens": output_budget,
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt + repair}],
                "response_format": response_format,
            }
            url = config["base_url"] + "/chat/completions"
        try:
            raw, request_id, http_status = _request_json(url, payload, headers, timeout)
            content, envelope = _gemini_content(raw) if provider == "gemini" else _json_content(raw)
            try:
                parsed = json.loads(content) if content else None
            except json.JSONDecodeError as exc:
                last_error = f"JSONDecodeError: {exc}"
                attempts.append({
                    "attempt": attempt_number, "api_surface": config["api_surface"],
                    "structured_mode": structured_mode,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "http_status": http_status, "request_id": request_id or raw.get("id"),
                    "requested_model": config["model"], "returned_model": raw.get("model"),
                    "usage": raw.get("usage") or raw.get("usageMetadata") or {},
                    "content_present": True, "content_excerpt": _redact(content)[:MAX_RAW_EXCERPT],
                    "schema_valid": False, "validation_errors": [_redact(last_error)], **envelope,
                })
                continue
            parsed = redact_sensitive(parsed)
            validation_errors = schema_errors(parsed, schema) if parsed is not None else ["empty content"]
            valid = not validation_errors
            attempt = {
                "attempt": attempt_number, "api_surface": config["api_surface"], "structured_mode": structured_mode,
                "latency_ms": int((time.monotonic() - started) * 1000), "http_status": http_status,
                "request_id": request_id or raw.get("id"), "requested_model": config["model"], "returned_model": raw.get("model"),
                "usage": raw.get("usage") or raw.get("usageMetadata") or {}, "content_present": bool(content),
                "content_excerpt": _redact(content)[:MAX_RAW_EXCERPT], "schema_valid": valid,
                "schema_errors": validation_errors[:20], **envelope,
            }
            attempts.append(attempt)
            if parsed is not None:
                artifacts.append({
                    "attempt": attempt_number,
                    "payload": parsed,
                    "validation_errors": validation_errors,
                })
            if valid:
                provenance = {
                    "provider": provider, "endpoint_alias": "local" if provider == "local" else provider,
                    "requested_model": config["model"], "returned_model": raw.get("model"),
                    "api_surface": config["api_surface"], "prompt_version": prompt_version,
                    "prompt_hash": prompt_hash,
                    "input_token_estimate": estimate_tokens(system_prompt + user_prompt),
                    "capability_probe": capability, "attempt_count": attempt_number,
                    "usage": attempt["usage"], "finish_reason": envelope.get("finish_reason"),
                    "fallback": {
                        "used": bool(context.get("fallback_from_call_id")),
                        "from_call_id": context.get("fallback_from_call_id"),
                    },
                }
                if call_id is not None:
                    provenance["semantic_call_id"] = str(call_id)
                artifact_ids = semantic_persistence.finish_model_call(
                    context, call_id, status="complete", provenance=provenance,
                    attempts=attempts, artifacts=artifacts,
                ) if semantic_persistence is not None else ()
                if artifact_ids:
                    provenance["semantic_artifact_id"] = str(artifact_ids[-1])
                return StructuredResult(
                    parsed, provenance, attempts, semantic_call_id=call_id,
                    artifact_ids=artifact_ids,
                )
            last_error = "model returned empty content" if not content else "model output failed schema validation: " + "; ".join(validation_errors[:5])
            if envelope.get("finish_reason") in {"length", "max_tokens", "MAX_TOKENS"} or (not content and envelope.get("reasoning_excerpt")):
                output_budget = min(output_budget * 2, 32768)
        except (RuntimeError, URLError, TimeoutError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            attempts.append({
                "attempt": attempt_number, "api_surface": config["api_surface"], "structured_mode": structured_mode,
                "latency_ms": int((time.monotonic() - started) * 1000), "error": _redact(last_error)[:MAX_RAW_EXCERPT],
            })
    provenance = {
        "provider": provider, "endpoint_alias": "local" if provider == "local" else provider,
        "requested_model": config["model"], "api_surface": config["api_surface"],
        "prompt_version": prompt_version, "prompt_hash": prompt_hash,
        "capability_probe": capability,
        "input_token_estimate": estimate_tokens(system_prompt + user_prompt), "attempt_count": len(attempts),
        "fallback": {
            "used": bool(context.get("fallback_from_call_id")),
            "from_call_id": context.get("fallback_from_call_id"),
        },
    }
    if call_id is not None:
        provenance["semantic_call_id"] = str(call_id)
    final_error = last_error or "structured-output call failed"
    artifact_ids = semantic_persistence.finish_model_call(
        context, call_id, status="failed", provenance=provenance,
        attempts=attempts, artifacts=artifacts, error=final_error,
    ) if semantic_persistence is not None else ()
    return StructuredResult(
        None, provenance, attempts, final_error, semantic_call_id=call_id,
        artifact_ids=artifact_ids,
    )
