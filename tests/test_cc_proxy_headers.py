from cc_proxy.headers import build_claude_code_headers, build_passthrough_headers


def test_build_claude_code_headers_removes_sdk_and_api_key_headers() -> None:
    headers = build_claude_code_headers(
        {
            "anthropic-version": "2023-06-01",
            "x-api-key": "sk-test",
            "x-stainless-lang": "python",
            "x-stainless-package-version": "1.2.3",
            "user-agent": "Anthropic/Python 0.1",
            "host": "127.0.0.1:8790",
            "x-tg-proxy-rid": "r_internal",
        },
        user_agent="claude-cli/test (external, cli)",
    )

    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["user-agent"] == "claude-cli/test (external, cli)"
    assert headers["x-app"] == "cli"
    assert headers["accept-encoding"] == "identity"
    assert "x-api-key" not in headers
    assert "x-stainless-lang" not in headers
    assert "x-stainless-package-version" not in headers
    assert "host" not in headers
    assert "x-tg-proxy-rid" not in headers


def test_build_claude_code_headers_prefers_existing_authorization() -> None:
    headers = build_claude_code_headers(
        {
            "Authorization": "Bearer oat-token",
            "x-api-key": "sk-test",
            "anthropic-beta": "messages-2023-12-15",
        },
        user_agent="claude-cli/test (external, cli)",
    )

    assert headers["authorization"] == "Bearer oat-token"
    assert headers["anthropic-beta"] == "messages-2023-12-15"
    assert "x-api-key" not in headers


def test_build_claude_code_headers_overrides_identity_headers() -> None:
    headers = build_claude_code_headers(
        {
            "accept-encoding": "gzip",
            "x-app": "sdk",
            "User-Agent": "custom-client",
            "accept": "text/event-stream",
            "content-type": "application/json",
        },
        user_agent="claude-cli/test (external, cli)",
    )

    assert headers["accept-encoding"] == "identity"
    assert headers["x-app"] == "cli"
    assert headers["user-agent"] == "claude-cli/test (external, cli)"
    assert headers["accept"] == "text/event-stream"
    assert headers["content-type"] == "application/json"


def test_build_passthrough_headers_keeps_client_identity_headers() -> None:
    headers = build_passthrough_headers(
        {
            "Authorization": "Bearer oat-token",
            "x-api-key": "sk-test",
            "x-stainless-lang": "python",
            "user-agent": "Anthropic/Python 0.1",
            "x-app": "sdk",
            "accept-encoding": "gzip",
            "host": "127.0.0.1:8790",
            "content-length": "123",
            "x-tg-proxy-rid": "r_internal",
            "connection": "keep-alive",
        }
    )

    assert headers["authorization"] == "Bearer oat-token"
    assert headers["x-api-key"] == "sk-test"
    assert headers["x-stainless-lang"] == "python"
    assert headers["user-agent"] == "Anthropic/Python 0.1"
    assert headers["x-app"] == "sdk"
    assert headers["accept-encoding"] == "gzip"
    assert "host" not in headers
    assert "content-length" not in headers
    assert "x-tg-proxy-rid" not in headers
    assert "connection" not in headers
