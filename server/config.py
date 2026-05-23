import os
from urllib.parse import urlparse


def configured_anthropic_base_url() -> str:
    configured = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip()
    return (configured or "https://api.anthropic.com").rstrip("/")


def cc_proxy_base_url() -> str:
    host = os.environ.get("CC_PROXY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("CC_PROXY_PORT", "8790").strip() or "8790"
    if "://" in host:
        base = host.rstrip("/")
    else:
        base = f"http://{host}"
    parsed = urlparse(base)
    if parsed.port is not None or not port:
        return base
    return f"{base}:{port}"
