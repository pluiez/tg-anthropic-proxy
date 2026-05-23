def anthropic_v1_path(route_path: str) -> str:
    """Return the upstream Anthropic v1 path for a client route suffix.

    Claude Code users sometimes configure ANTHROPIC_BASE_URL with a trailing
    /v1 while the SDK still appends /v1/messages. Accept that shape locally but
    forward only one /v1 segment upstream.
    """
    normalized = route_path.lstrip("/")
    if normalized == "v1":
        normalized = ""
    elif normalized.startswith("v1/"):
        normalized = normalized[3:]
    if normalized:
        return f"/v1/{normalized}"
    return "/v1"
