import json
from collections.abc import Iterable, Mapping
from typing import Any


_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}


def redact_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    items = headers.items() if isinstance(headers, Mapping) else headers
    redacted: dict[str, str] = {}
    for name, value in items:
        lower_name = name.lower()
        if lower_name in _SENSITIVE_HEADERS:
            redacted[lower_name] = _redacted_value(lower_name, value)
        else:
            redacted[lower_name] = value
    return redacted


def summarize_json_body(body: bytes) -> dict[str, Any]:
    summary: dict[str, Any] = {"bytes": len(body)}
    if not body:
        return summary
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        summary["json"] = False
        return summary

    summary["json"] = True
    if not isinstance(payload, dict):
        summary["json_type"] = type(payload).__name__
        return summary

    for key in ("model", "stream", "max_tokens", "temperature", "top_p"):
        if key in payload:
            summary[key] = payload[key]

    messages = payload.get("messages")
    if isinstance(messages, list):
        summary["messages"] = len(messages)
        summary["message_roles"] = [
            message.get("role")
            for message in messages
            if isinstance(message, dict) and "role" in message
        ]

    tools = payload.get("tools")
    if isinstance(tools, list):
        summary["tools"] = len(tools)

    system = payload.get("system")
    if system is not None:
        summary["has_system"] = True

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        summary["metadata_keys"] = sorted(str(key) for key in metadata)

    return summary


def _redacted_value(name: str, value: str) -> str:
    if name == "authorization" and value.lower().startswith("bearer "):
        token = value[7:]
        return f"Bearer <redacted len={len(token)}>"
    return f"<redacted len={len(value)}>"
