from shared.cache_protocol import (
    CACHE_REF_KEY,
    MESSAGES_CACHE_KEY,
    cache_candidates_for_body,
    cache_key_for_json,
    compress_body_with_cache_refs,
    parse_cache_fields,
    restore_body_from_cache_refs,
)


def _body() -> dict:
    return {
        "model": "claude-opus-4-7",
        "tools": [
            {"name": "tool_a", "description": "A" * 64, "input_schema": {"type": "object"}},
            {"name": "tool_b", "description": "B" * 64, "input_schema": {"type": "object"}},
        ],
        "system": [{"type": "text", "text": "system prompt " * 20}],
        "messages": [
            {"role": "user", "content": "first " * 40},
            {"role": "assistant", "content": "second " * 40},
            {"role": "user", "content": "third"},
        ],
    }


def test_cache_key_is_bare_64_hex_digest() -> None:
    key = cache_key_for_json({"b": 1, "a": [2, 3]})

    assert len(key) == 64
    assert not key.startswith("sha256:")
    int(key, 16)


def test_cache_candidates_include_tools_system_and_message_prefixes() -> None:
    candidates = cache_candidates_for_body(_body(), min_bytes=1)

    kinds = [(candidate.kind, candidate.prefix_len) for candidate in candidates]
    assert ("tools", None) in kinds
    assert ("system", None) in kinds
    assert ("messages", 1) in kinds
    assert ("messages", 2) in kinds
    assert ("messages", 3) in kinds


def test_compress_and_restore_uses_known_top_level_and_longest_message_prefix() -> None:
    body = _body()
    candidates = cache_candidates_for_body(body, min_bytes=1)
    tools = next(candidate for candidate in candidates if candidate.kind == "tools")
    system = next(candidate for candidate in candidates if candidate.kind == "system")
    messages_1 = next(candidate for candidate in candidates if candidate.kind == "messages" and candidate.prefix_len == 1)
    messages_2 = next(candidate for candidate in candidates if candidate.kind == "messages" and candidate.prefix_len == 2)
    store = {candidate.key: candidate.data for candidate in candidates}

    result = compress_body_with_cache_refs(
        body,
        {tools.key, system.key, messages_1.key, messages_2.key},
        min_bytes=1,
    )

    assert result is not None
    assert result.messages_prefix_len == 2
    assert result.body["tools"][CACHE_REF_KEY]["key"] == tools.key
    assert result.body["system"][CACHE_REF_KEY]["key"] == system.key
    assert result.body["messages"][MESSAGES_CACHE_KEY]["prefix"]["key"] == messages_2.key
    assert result.body["messages"][MESSAGES_CACHE_KEY]["tail"] == body["messages"][2:]

    restored = restore_body_from_cache_refs(result.body, store.get)

    assert restored.missing_keys == []
    assert set(restored.used_keys) == {tools.key, system.key, messages_2.key}
    assert restored.body == body


def test_restore_reports_cache_miss_without_mutating_tail() -> None:
    body = _body()
    messages_1 = next(
        candidate
        for candidate in cache_candidates_for_body(body, min_bytes=1)
        if candidate.kind == "messages" and candidate.prefix_len == 1
    )
    result = compress_body_with_cache_refs(body, {messages_1.key}, min_bytes=1)

    assert result is not None
    restored = restore_body_from_cache_refs(result.body, lambda key: None)

    assert restored.missing_keys == [messages_1.key]
    assert restored.used_keys == []


def test_parse_cache_fields_ignores_unknown_fields() -> None:
    assert parse_cache_fields("tools,messages,metadata") == frozenset({"tools", "messages"})
