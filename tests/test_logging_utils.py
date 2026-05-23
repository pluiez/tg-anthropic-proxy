from shared.logging_utils import redact_headers, summarize_json_body


def test_redact_headers_hides_sensitive_values() -> None:
    headers = redact_headers(
        {
            "Authorization": "Bearer secret-token",
            "x-api-key": "sk-secret",
            "anthropic-version": "2023-06-01",
        }
    )

    assert headers["authorization"] == "Bearer <redacted len=12>"
    assert headers["x-api-key"] == "<redacted len=9>"
    assert headers["anthropic-version"] == "2023-06-01"


def test_summarize_json_body_omits_message_content() -> None:
    summary = summarize_json_body(
        b'{"model":"claude-test","stream":true,"max_tokens":10,'
        b'"messages":[{"role":"user","content":"secret prompt"}]}'
    )

    assert summary["bytes"] > 0
    assert summary["json"] is True
    assert summary["model"] == "claude-test"
    assert summary["stream"] is True
    assert summary["max_tokens"] == 10
    assert summary["messages"] == 1
    assert summary["message_roles"] == ["user"]
    assert "secret prompt" not in repr(summary)
