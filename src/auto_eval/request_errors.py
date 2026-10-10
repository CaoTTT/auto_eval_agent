"""Classify permanent request limits without confusing them with quota throttling."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_CONTEXT_CODES = {
    "context_length_exceeded", "context_window_exceeded", "context_limit_exceeded",
    "context_budget_exceeded", "input_tokens_exceeded", "input_token_limit_exceeded",
    "prompt_too_long", "input_too_long", "too_many_input_tokens",
}
_LOCAL_LIMIT_CODES = {
    "request_image_limit_exceeded", "request_image_count_exceeded",
    "request_payload_bytes_exceeded", "conversation_request_budget_exceeded",
}
_CONTEXT_PATTERNS = (
    r"(?:context[_\s-]*(?:length|window|limit|size|budget)).{0,100}(?:exceed|too\s+(?:long|large)|over|maximum|limit)",
    r"(?:exceed|over).{0,80}(?:context[_\s-]*(?:length|window|limit|size|budget))",
    r"maximum context length.{0,160}(?:requested|resulted|messages|tokens)",
    r"(?:input|prompt)[_\s-]*(?:token(?:s)?|length|size).{0,100}(?:exceed|too\s+(?:long|large)|beyond)",
    r"(?:input|prompt).{0,50}(?:too long|too large|longer than|must be (?:less|shorter)|should be.{0,20}\[\s*1\s*[,，])",
    r"(?:total|combined).{0,30}tokens.{0,80}(?:exceed|maximum|context)",
    r"(?:上下文|输入|提示词|提示文本).{0,25}(?:长度|token|令牌|tokens|大小).{0,40}(?:超出|超过|超限|过长|大于|上限)",
    r"(?:超过|超出|超限).{0,30}(?:上下文|输入.{0,15}(?:token|令牌|长度))",
)
_SAFE_ERROR_KEYS = {
    "error", "errors", "message", "detail", "code", "type", "status", "status_code",
    "http_status", "request_id", "requestId",
    "data", "response", "body", "result",
}


def _safe_text(value: Any, limit: int = 3000) -> str:
    text = str(value)
    text = re.sub(r"data:[^\s\"']+", "[image data omitted]", text, flags=re.I)
    text = re.sub(r"Bearer\s+[^\s\"']+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[redacted]", text)
    return text[:limit]


def _safe_payload(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {key: _safe_payload(item, depth + 1) for key, item in value.items()
                if key in _SAFE_ERROR_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe_payload(item, depth + 1) for item in value[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _safe_text(value) if isinstance(value, str) else value
    dumped = getattr(value, "model_dump", None)
    if callable(dumped):
        return _safe_payload(dumped(), depth + 1)
    return {key: _safe_payload(getattr(value, key), depth + 1)
            for key in _SAFE_ERROR_KEYS if getattr(value, key, None) is not None}


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain, visited = [], set()
    cause = exc
    while cause is not None and id(cause) not in visited and len(visited) < 5:
        chain.append(cause)
        visited.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return chain


def _payload_field(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if payload.get(key) is not None and isinstance(payload[key], (str, int, float)):
            return payload[key]
        for value in payload.values():
            found = _payload_field(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _payload_field(value, key)
            if found is not None:
                return found
    return None


def _signals(exc: BaseException) -> tuple[str, dict, int | None]:
    payload, status, messages = {}, None, []
    for cause in _exception_chain(exc):
        body = getattr(cause, "body", None)
        response = getattr(cause, "response", None)
        if body is None and response is not None:
            try:
                body = response.json()
            except Exception:
                pass
        safe_body = _safe_payload(body) or {}
        payload = payload or safe_body
        status = status or getattr(cause, "status_code", None) or getattr(response, "status_code", None)
        messages.extend([_safe_text(cause), json.dumps(safe_body, ensure_ascii=False)])
    return " ".join(messages).lower(), payload, status


def nonretryable_context_error(exc: BaseException) -> bool:
    """Recognize an input/context overflow, never an output cutoff or TPM quota."""
    code = str(getattr(exc, "code", "")).lower()
    chain = _exception_chain(exc)
    report = next((getattr(cause, "request_budget_report", None) for cause in chain
                   if getattr(cause, "request_budget_report", None)), {})
    if code in _CONTEXT_CODES or "context_budget_exceeded" in report.get("blocking_reasons", []):
        return True
    if any(type(cause).__name__ == "ContextBudgetExceeded" for cause in chain):
        return True
    if any(getattr(cause, "code", None) in _LOCAL_LIMIT_CODES for cause in chain) and report:
        # A local report is authoritative; its explanatory message also lists
        # context limits even when only an image-count/byte limit was exceeded.
        return False
    signal, _, _ = _signals(exc)
    explicit_context = any(word in signal for word in ("context length", "context_length", "context window", "context_window", "上下文"))
    if not explicit_context and any(word in signal for word in (
        "tokens per minute", "tokens-per-minute", "token per minute", " tpm", '"tpm"',
        "rate_limit", "rate limit", "token rate", "quota", "限流", "每分钟", "配额",
    )):
        return False
    if any(re.search(r"(?<![a-z0-9_])" + re.escape(marker) + r"(?![a-z0-9_])", signal) for marker in _CONTEXT_CODES):
        return True
    return any(re.search(pattern, signal, re.I | re.S) for pattern in _CONTEXT_PATTERNS)


def is_nonretryable_request_limit(exc: BaseException) -> bool:
    if nonretryable_context_error(exc):
        return True
    _, _, status = _signals(exc)
    return status == 413 or any(getattr(cause, "code", None) in _LOCAL_LIMIT_CODES for cause in _exception_chain(exc))


def request_limit_diagnostics(
    exc: BaseException, *, model: str | None = None, request_budget_report: dict | None = None,
) -> dict | None:
    """Return safe, durable diagnostics; callers may persist them with a failed case."""
    if not is_nonretryable_request_limit(exc):
        return None
    _, payload, status = _signals(exc)
    chain = _exception_chain(exc)
    report = next((getattr(cause, "request_budget_report", None) for cause in chain
                   if getattr(cause, "request_budget_report", None)), None) or request_budget_report
    provider_response = status is not None or any(getattr(cause, "response", None) is not None or
                                                getattr(cause, "body", None) is not None for cause in chain)
    local = any(type(cause).__name__ in {"QueryImageError", "ContextBudgetExceeded"} for cause in chain) or (
        not provider_response and any(getattr(cause, "code", None) in _CONTEXT_CODES | _LOCAL_LIMIT_CODES for cause in chain)
    )
    context = nonretryable_context_error(exc)
    provider_code = _payload_field(payload, "code")
    provider_request_id = _payload_field(payload, "request_id") or _payload_field(payload, "requestId")
    headers = next((getattr(getattr(cause, "response", None), "headers", None) for cause in chain
                    if getattr(getattr(cause, "response", None), "headers", None)), {})
    provider_request_id = provider_request_id or headers.get("x-request-id") or headers.get("x-dashscope-request-id")
    local_code = next((getattr(cause, "code", None) for cause in chain if getattr(cause, "code", None) in _LOCAL_LIMIT_CODES), None)
    code = ("context_budget_exceeded" if local else "context_limit_exceeded") if context else (local_code or "request_payload_bytes_exceeded")
    reason_parts = [_safe_text(cause, 1500) for cause in chain]
    provider_reason = _payload_field(payload, "message") or _payload_field(payload, "detail") or provider_code
    if provider_reason and str(provider_reason) not in reason_parts:
        reason_parts.append(_safe_text(provider_reason))
    result = {
        "code": code, "reason": "；原因：".join(reason_parts), "origin": "local_guardrail" if local else "provider",
        "model": model, "provider_status": status, "provider_code": provider_code,
        "provider_request_id": provider_request_id, "provider_error": payload,
        "severity": "warning", "status": "blocked", "retryable": False,
        "retry_allowed": False, "automatic_retry_skipped": True,
    }
    if report:
        result["request_budget_report"] = report
    result["error_id"] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return result
