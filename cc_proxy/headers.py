import os
from collections.abc import Mapping


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_REQUEST_DROP_HEADERS = _HOP_BY_HOP_HEADERS | {
    "content-length",
    "host",
    "x-api-key",
    "x-tg-proxy-rid",
}

_FORCED_HEADERS = {
    "accept-encoding",
    "user-agent",
    "x-app",
}


def claude_code_user_agent() -> str:
    configured = os.getenv("CC_PROXY_CLAUDE_CODE_USER_AGENT", "").strip()
    if configured:
        return configured
    version = os.getenv("CC_PROXY_CLAUDE_CODE_VERSION", "1.0.0").strip() or "1.0.0"
    return f"claude-cli/{version} (external, cli)"


def build_claude_code_headers(
    incoming: Mapping[str, str],
    *,
    user_agent: str | None = None,
) -> dict[str, str]:
    """Return upstream request headers with Claude Code's request fingerprint.

    The optional proxy always applies this policy. It does not inspect the
    upstream host because deploy-time configuration decides where requests go.
    """
    outbound: dict[str, str] = {}
    api_key: str | None = None
    authorization: str | None = None

    for name, value in incoming.items():
        lower_name = name.lower()
        if lower_name.startswith("x-stainless-"):
            continue
        if lower_name == "x-api-key":
            api_key = value
            continue
        if lower_name == "authorization":
            authorization = value
            continue
        if lower_name in _REQUEST_DROP_HEADERS or lower_name in _FORCED_HEADERS:
            continue
        outbound[lower_name] = value

    if authorization:
        outbound["authorization"] = authorization
    elif api_key:
        if api_key.lower().startswith("bearer "):
            outbound["authorization"] = api_key
        else:
            outbound["authorization"] = f"Bearer {api_key}"

    outbound["user-agent"] = user_agent or claude_code_user_agent()
    outbound["x-app"] = "cli"
    outbound["accept-encoding"] = "identity"
    return outbound
