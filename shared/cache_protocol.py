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
    return "sha256:" + hashlib.sha256(data).hexdigest()


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


def _candidate(kind: str, value: Any, *, prefix_len: int | None = None) -> CacheCandidate:
    data = canonical_json_bytes(value)
    return CacheCandidate(
        key=cache_key_for_bytes(data),
        kind=kind,
        size=len(data),
        data=data,
        prefix_len=prefix_len,
    )


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

    for field in ("tools", "system"):
        if field not in enabled or field not in body:
            continue
        candidate = _candidate(field, body[field])
        if candidate.size >= min_bytes:
            candidates.append(candidate)

    messages = body.get("messages")
    if "messages" in enabled and isinstance(messages, list):
        for prefix_len in range(1, len(messages) + 1):
            candidate = _candidate("messages", messages[:prefix_len], prefix_len=prefix_len)
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
    by_field = {candidate.kind: candidate for candidate in candidates if candidate.prefix_len is None}
    message_prefixes = [candidate for candidate in candidates if candidate.kind == "messages"]

    compressed = dict(body)
    ref_keys: list[str] = []

    for field in ("tools", "system"):
        candidate = by_field.get(field)
        if candidate is not None and candidate.key in known_keys:
            compressed[field] = _ref_for_candidate(candidate)
            ref_keys.append(candidate.key)

    known_message_prefixes = [
        candidate for candidate in message_prefixes if candidate.key in known_keys and candidate.prefix_len is not None
    ]
    messages_prefix_len = 0
    if known_message_prefixes and isinstance(body.get("messages"), list):
        chosen = max(known_message_prefixes, key=lambda candidate: candidate.prefix_len or 0)
        messages_prefix_len = int(chosen.prefix_len or 0)
        compressed["messages"] = {
            MESSAGES_CACHE_KEY: {
                "prefix": _ref_for_candidate(chosen)[CACHE_REF_KEY],
                "tail": body["messages"][messages_prefix_len:],
            }
        }
        ref_keys.append(chosen.key)

    return CacheCompressionResult(
        body=compressed,
        candidates=candidates,
        ref_keys=ref_keys,
        messages_prefix_len=messages_prefix_len,
    )


def restore_body_from_cache_refs(
    body: Any,
    cache_get: Callable[[str], bytes | None],
) -> CacheRestoreResult:
    if not isinstance(body, dict):
        raise ValueError("cache-ref body must be a JSON object")

    restored = dict(body)
    missing: list[str] = []
    used: list[str] = []

    for field in ("tools", "system"):
        ref = _parse_ref(restored.get(field))
        if ref is None:
            continue
        key = ref["key"]
        data = cache_get(key)
        if data is None:
            missing.append(key)
            continue
        restored[field] = json_bytes_to_value(data)
        used.append(key)

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

    return CacheRestoreResult(body=restored, missing_keys=missing, used_keys=used)
