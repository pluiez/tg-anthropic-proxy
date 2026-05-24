import copy
import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

CACHE_REF_KEY = "$tg_cache_ref"
MESSAGES_CACHE_KEY = "$tg_cache_messages"
DEFAULT_CACHE_FIELDS = frozenset({"tools", "system", "messages"})
DEFAULT_CACHE_TTL_SECONDS = 72 * 60 * 60
DEFAULT_CACHE_MIN_BYTES = 2048
DEFAULT_CACHE_MAX_ITEMS = 256
DEFAULT_CACHE_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class CacheCandidate:
    key: str
    kind: str
    size: int
    data: bytes
    prefix_len: int | None = None
    path: tuple[str | int, ...] = ()


@dataclass(frozen=True)
class CacheCompressionResult:
    body: dict[str, Any]
    candidates: list[CacheCandidate]
    ref_keys: list[str]
    messages_prefix_len: int


@dataclass(frozen=True)
class CacheRestoreResult:
    body: dict[str, Any]
    missing_keys: list[str]
    used_keys: list[str]


def parse_cache_fields(raw: str | None) -> frozenset[str]:
    if raw is None or not raw.strip():
        return DEFAULT_CACHE_FIELDS
    fields = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset(fields & DEFAULT_CACHE_FIELDS)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def cache_key_for_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_key_for_json(value: Any) -> str:
    return cache_key_for_bytes(canonical_json_bytes(value))


def json_bytes_to_value(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"))


def body_json_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _candidate(
    kind: str,
    value: Any,
    *,
    prefix_len: int | None = None,
    path: tuple[str | int, ...] = (),
) -> CacheCandidate:
    data = canonical_json_bytes(value)
    return CacheCandidate(
        key=cache_key_for_bytes(data),
        kind=kind,
        size=len(data),
        data=data,
        prefix_len=prefix_len,
        path=path,
    )


def _is_content_block(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("type"), str) and CACHE_REF_KEY not in value


def cache_candidates_for_body(
    body: Any,
    *,
    min_bytes: int = DEFAULT_CACHE_MIN_BYTES,
    fields: Iterable[str] = DEFAULT_CACHE_FIELDS,
) -> list[CacheCandidate]:
    if not isinstance(body, dict):
        return []

    enabled = set(fields)
    min_bytes = max(0, int(min_bytes))
    candidates: list[CacheCandidate] = []

    if "tools" in enabled and "tools" in body:
        candidate = _candidate("tools", body["tools"], path=("tools",))
        if candidate.size >= min_bytes:
            candidates.append(candidate)

    system = body.get("system")
    if "system" in enabled and isinstance(system, list):
        for index, block in enumerate(system):
            if not _is_content_block(block):
                continue
            candidate = _candidate("system_block", block, path=("system", index))
            if candidate.size >= min_bytes:
                candidates.append(candidate)

    messages = body.get("messages")
    if "messages" in enabled and isinstance(messages, list):
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if not _is_content_block(block):
                    continue
                candidate = _candidate(
                    "message_content_block",
                    block,
                    path=("messages", message_index, "content", block_index),
                )
                if candidate.size >= min_bytes:
                    candidates.append(candidate)

    return candidates


def _ref_for_candidate(candidate: CacheCandidate) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "key": candidate.key,
        "kind": candidate.kind,
        "size": candidate.size,
    }
    if candidate.prefix_len is not None:
        ref["prefix_len"] = candidate.prefix_len
    return {CACHE_REF_KEY: ref}


def _parse_ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    ref = value.get(CACHE_REF_KEY)
    if not isinstance(ref, dict):
        return None
    key = ref.get("key")
    if not isinstance(key, str) or not key:
        return None
    return ref


def _replace_path(root: dict[str, Any], path: tuple[str | int, ...], value: Any) -> None:
    target: Any = root
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def compress_body_with_cache_refs(
    body: Any,
    known_keys: set[str] | frozenset[str],
    *,
    min_bytes: int = DEFAULT_CACHE_MIN_BYTES,
    fields: Iterable[str] = DEFAULT_CACHE_FIELDS,
) -> CacheCompressionResult | None:
    if not isinstance(body, dict):
        return None

    candidates = cache_candidates_for_body(body, min_bytes=min_bytes, fields=fields)
    by_field = {candidate.kind: candidate for candidate in candidates if candidate.path == ("tools",)}
    block_candidates = [
        candidate
        for candidate in candidates
        if candidate.kind in {"system_block", "message_content_block"} and candidate.path
    ]

    known_block_candidates = [candidate for candidate in block_candidates if candidate.key in known_keys]
    compressed = copy.deepcopy(body) if known_block_candidates else dict(body)
    ref_keys: list[str] = []

    tools = by_field.get("tools")
    if tools is not None and tools.key in known_keys:
        compressed["tools"] = _ref_for_candidate(tools)
        ref_keys.append(tools.key)

    for candidate in known_block_candidates:
        _replace_path(compressed, candidate.path, _ref_for_candidate(candidate))
        ref_keys.append(candidate.key)

    return CacheCompressionResult(
        body=compressed,
        candidates=candidates,
        ref_keys=ref_keys,
        messages_prefix_len=0,
    )


def _restore_ref_value(ref: dict[str, Any], cache_get: Callable[[str], bytes | None]) -> tuple[Any, str | None]:
    key = ref["key"]
    data = cache_get(key)
    if data is None:
        return None, key
    return json_bytes_to_value(data), None


def restore_body_from_cache_refs(
    body: Any,
    cache_get: Callable[[str], bytes | None],
) -> CacheRestoreResult:
    if not isinstance(body, dict):
        raise ValueError("cache-ref body must be a JSON object")

    restored = copy.deepcopy(body)
    missing: list[str] = []
    used: list[str] = []

    for field in ("tools", "system"):
        ref = _parse_ref(restored.get(field))
        if ref is None:
            continue
        value, missing_key = _restore_ref_value(ref, cache_get)
        if missing_key is not None:
            missing.append(missing_key)
            continue
        restored[field] = value
        used.append(ref["key"])

    system = restored.get("system")
    if isinstance(system, list):
        for index, block in enumerate(system):
            ref = _parse_ref(block)
            if ref is None:
                continue
            value, missing_key = _restore_ref_value(ref, cache_get)
            if missing_key is not None:
                missing.append(missing_key)
                continue
            if not isinstance(value, dict):
                raise ValueError("cached system content block is not an object")
            system[index] = value
            used.append(ref["key"])

    messages_value = restored.get("messages")
    if isinstance(messages_value, dict) and MESSAGES_CACHE_KEY in messages_value:
        payload = messages_value.get(MESSAGES_CACHE_KEY)
        if not isinstance(payload, dict):
            raise ValueError("invalid messages cache wrapper")
        prefix_ref = payload.get("prefix")
        if not isinstance(prefix_ref, dict) or not isinstance(prefix_ref.get("key"), str):
            raise ValueError("invalid messages prefix cache ref")
        tail = payload.get("tail", [])
        if not isinstance(tail, list):
            raise ValueError("messages cache tail must be a list")

        key = prefix_ref["key"]
        data = cache_get(key)
        if data is None:
            missing.append(key)
        else:
            prefix = json_bytes_to_value(data)
            if not isinstance(prefix, list):
                raise ValueError("cached messages prefix is not a list")
            restored["messages"] = prefix + tail
            used.append(key)

    messages = restored.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for index, block in enumerate(content):
                ref = _parse_ref(block)
                if ref is None:
                    continue
                value, missing_key = _restore_ref_value(ref, cache_get)
                if missing_key is not None:
                    missing.append(missing_key)
                    continue
                if not isinstance(value, dict):
                    raise ValueError("cached message content block is not an object")
                content[index] = value
                used.append(ref["key"])

    return CacheRestoreResult(body=restored, missing_keys=missing, used_keys=used)
