import json

from shared.cache_protocol import (
    CACHE_REF_KEY,
    MESSAGES_CACHE_KEY,
    cache_candidates_for_body,
    cache_key_for_json,
    compress_body_with_cache_refs,
    parse_cache_fields,
    restore_body_from_cache_refs,
)


def _image_block(seed: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": seed * 80,
        },
    }


def _body() -> dict:
    return {
        "model": "claude-opus-4-7",
        "tools": [
            {"name": "tool_a", "description": "A" * 64, "input_schema": {"type": "object"}},
            {"name": "tool_b", "description": "B" * 64, "input_schema": {"type": "object"}},
        ],
        "system": [
            {"type": "text", "text": "volatile cch=abcde"},
            {"type": "text", "text": "system prompt " * 20},
            _image_block("s"),
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first " * 40},
                    _image_block("m"),
                ],
            },
            {"role": "assistant", "content": "string content is not a content block"},
        ],
    }


def test_cache_key_is_bare_64_hex_digest() -> None:
    key = cache_key_for_json({"b": 1, "a": [2, 3]})

    assert len(key) == 64
    assert not key.startswith("sha256:")
    int(key, 16)


def test_cache_candidates_include_tools_and_content_blocks() -> None:
    candidates = cache_candidates_for_body(_body(), min_bytes=1)

    candidates_by_path = {(candidate.kind, candidate.path) for candidate in candidates}
    assert ("tools", ("tools",)) in candidates_by_path
    assert ("system_block", ("system", 0)) in candidates_by_path
    assert ("system_block", ("system", 1)) in candidates_by_path
    assert ("system_block", ("system", 2)) in candidates_by_path
    assert ("message_content_block", ("messages", 0, "content", 0)) in candidates_by_path
    assert ("message_content_block", ("messages", 0, "content", 1)) in candidates_by_path
    assert not any(candidate.kind == "messages" for candidate in candidates)


def test_compress_and_restore_uses_known_top_level_and_content_blocks() -> None:
    body = _body()
    candidates = cache_candidates_for_body(body, min_bytes=1)
    tools = next(candidate for candidate in candidates if candidate.kind == "tools")
    stable_system = next(candidate for candidate in candidates if candidate.path == ("system", 1))
    message_image = next(
        candidate for candidate in candidates if candidate.path == ("messages", 0, "content", 1)
    )
    store = {candidate.key: candidate.data for candidate in candidates}

    result = compress_body_with_cache_refs(
        body,
        {tools.key, stable_system.key, message_image.key},
        min_bytes=1,
    )

    assert result is not None
    assert result.messages_prefix_len == 0
    assert result.body["tools"][CACHE_REF_KEY]["key"] == tools.key
    assert result.body["system"][0] == body["system"][0]
    assert result.body["system"][1][CACHE_REF_KEY]["key"] == stable_system.key
    assert result.body["messages"][0]["content"][0] == body["messages"][0]["content"][0]
    assert result.body["messages"][0]["content"][1][CACHE_REF_KEY]["key"] == message_image.key

    restored = restore_body_from_cache_refs(result.body, store.get)

    assert restored.missing_keys == []
    assert set(restored.used_keys) == {tools.key, stable_system.key, message_image.key}
    assert restored.body == body


def test_restore_reports_nested_content_block_cache_miss() -> None:
    body = _body()
    message_text = next(
        candidate
        for candidate in cache_candidates_for_body(body, min_bytes=1)
        if candidate.path == ("messages", 0, "content", 0)
    )
    result = compress_body_with_cache_refs(body, {message_text.key}, min_bytes=1)

    assert result is not None
    restored = restore_body_from_cache_refs(result.body, lambda key: None)

    assert restored.missing_keys == [message_text.key]
    assert restored.used_keys == []


def test_restore_supports_legacy_message_prefix_wrapper() -> None:
    body = _body()
    prefix = body["messages"][:1]
    tail = body["messages"][1:]
    key = "a" * 64
    wrapped = {
        **body,
        "messages": {
            MESSAGES_CACHE_KEY: {
                "prefix": {"key": key, "kind": "messages", "size": 1, "prefix_len": 1},
                "tail": tail,
            }
        },
    }

    restored = restore_body_from_cache_refs(wrapped, {key: json.dumps(prefix).encode("utf-8")}.get)

    assert restored.missing_keys == []
    assert restored.used_keys == [key]
    assert restored.body == body


def test_parse_cache_fields_ignores_unknown_fields() -> None:
    assert parse_cache_fields("tools,messages,metadata") == frozenset({"tools", "messages"})
