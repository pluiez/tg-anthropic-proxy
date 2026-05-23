from client.routing import anthropic_v1_path


def test_anthropic_v1_path_preserves_normal_messages_path() -> None:
    assert anthropic_v1_path("messages") == "/v1/messages"


def test_anthropic_v1_path_normalizes_duplicate_v1_prefix() -> None:
    assert anthropic_v1_path("v1/messages") == "/v1/messages"


def test_anthropic_v1_path_handles_base_v1_probe_suffix() -> None:
    assert anthropic_v1_path("v1") == "/v1"
