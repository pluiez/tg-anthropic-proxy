import base64
import gzip
import hashlib
import json
import uuid
from typing import Any, Optional

# raw bytes per frame. gzip + base64 + framing JSON 之后单条 Telegram 消息
# 必须 < 4096 字符 (Bot API 文本上限). 即便 gzip 完全压缩不动 (高熵 payload),
# 2400 raw bytes 经 gzip overhead (~10B) + base64 (4/3) ≈ 3214 字符,
# 加 framing JSON ~80 字符仍稳稳塞下.
MAX_RAW_CHUNK = 2400

# Telegram Bot API text messages are capped at 4096 characters. Hermes uses
# 4000 as its near-limit threshold for Telegram text splitting; use the same
# default here while still validating the actual encoded frame length.
TELEGRAM_TEXT_LIMIT_CHARS = 4096
TELEGRAM_CAPTION_LIMIT_CHARS = 1024
MIN_TEXT_FRAME_CHARS = 1024
MAX_TEXT_FRAME_CHARS = 4000
REQUEST_BLOB_KIND = "req_blob"
BLOB_ENCODING = "gzip"
_TOTAL_HINT = 999999


def new_request_id() -> str:
    return f"r_{uuid.uuid4().hex[:12]}"


def _encode(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data)).decode("ascii")


def _decode(s: str) -> bytes:
    return gzip.decompress(base64.b64decode(s.encode("ascii")))


def make_frame(
    rid: str,
    seq: int,
    kind: str,
    *,
    data: Optional[bytes] = None,
    **extra,
) -> str:
    obj = {"v": 1, "rid": rid, "seq": seq, "kind": kind, **extra}
    if data is not None:
        obj["data"] = _encode(data)
    return json.dumps(obj, separators=(",", ":"))


def parse_frame(text: str) -> Optional[dict]:
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    if "rid" not in d or "kind" not in d:
        return None
    if "data" in d:
        try:
            d["payload"] = _decode(d["data"])
        except (ValueError, OSError):
            return None
    return d


def chunk_bytes(data: bytes, size: int = MAX_RAW_CHUNK) -> list[bytes]:
    if not data:
        return [b""]
    return [data[i:i + size] for i in range(0, len(data), size)]


def coerce_text_frame_chars(value: str | int | None, default: int = MAX_TEXT_FRAME_CHARS) -> int:
    try:
        if value is None:
            parsed = int(default)
        elif isinstance(value, str):
            parsed = int(value) if value.strip() else int(default)
        else:
            parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(MIN_TEXT_FRAME_CHARS, min(parsed, TELEGRAM_TEXT_LIMIT_CHARS))


def chunk_bytes_for_frame_payloads(
    data: bytes,
    rid: str,
    kind: str,
    *,
    max_chars: int = MAX_TEXT_FRAME_CHARS,
    extra: Optional[dict] = None,
) -> list[bytes]:
    """Pack payload chunks using actual encoded frame length.

    Fixed raw chunking is safe but very inefficient for large, compressible JSON
    requests because each chunk is gzipped independently. This helper keeps the
    existing frame format while choosing the largest raw chunk whose encoded
    Telegram message stays below ``max_chars``.
    """
    max_chars = coerce_text_frame_chars(max_chars)
    if not data:
        return [b""]

    frame_extra = dict(extra or {})
    chunks: list[bytes] = []
    offset = 0
    while offset < len(data):
        remaining = len(data) - offset
        low = 1
        high = remaining
        best = 1
        seq = len(chunks)
        while low <= high:
            mid = (low + high) // 2
            candidate = data[offset:offset + mid]
            frame = make_frame(rid, seq, kind, data=candidate, **frame_extra)
            if len(frame) <= max_chars:
                best = mid
                low = mid + 1
            else:
                high = mid - 1

        chunk = data[offset:offset + best]
        if len(make_frame(rid, seq, kind, data=chunk, **frame_extra)) > max_chars:
            raise ValueError("single-byte frame exceeds Telegram text limit")
        chunks.append(chunk)
        offset += best

    return chunks


def chunk_request_envelope(
    envelope: bytes,
    rid: str,
    *,
    max_chars: int = MAX_TEXT_FRAME_CHARS,
) -> list[bytes]:
    # Use a conservative total hint while choosing chunks. The real total is no
    # longer than this in normal operation, so final frames are not larger.
    return chunk_bytes_for_frame_payloads(
        envelope,
        rid,
        "req",
        max_chars=max_chars,
        extra={"total": _TOTAL_HINT},
    )


def make_request_blob(rid: str, envelope: bytes) -> tuple[str, bytes]:
    """Encode a complete request envelope for one Telegram document message."""
    blob = gzip.compress(envelope)
    caption = json.dumps(
        {
            "v": 1,
            "rid": rid,
            "kind": REQUEST_BLOB_KIND,
            "encoding": BLOB_ENCODING,
            "raw_size": len(envelope),
            "sha256": hashlib.sha256(envelope).hexdigest(),
        },
        separators=(",", ":"),
    )
    if len(caption) > TELEGRAM_CAPTION_LIMIT_CHARS:
        raise ValueError("request blob caption exceeds Telegram caption limit")
    return caption, blob


def parse_request_blob_caption(caption: str | None) -> Optional[dict[str, Any]]:
    if not caption:
        return None
    try:
        d = json.loads(caption)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    if d.get("kind") != REQUEST_BLOB_KIND:
        return None
    if not isinstance(d.get("rid"), str):
        return None
    return d


def decode_request_blob(blob: bytes, metadata: dict[str, Any]) -> bytes:
    if metadata.get("encoding") != BLOB_ENCODING:
        raise ValueError(f"unsupported request blob encoding: {metadata.get('encoding')!r}")
    try:
        raw = gzip.decompress(blob)
    except (ValueError, OSError) as exc:
        raise ValueError("invalid request blob gzip payload") from exc

    expected_size = metadata.get("raw_size")
    if isinstance(expected_size, int) and expected_size != len(raw):
        raise ValueError(f"request blob size mismatch: expected {expected_size}, got {len(raw)}")

    expected_hash = metadata.get("sha256")
    if isinstance(expected_hash, str):
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError("request blob sha256 mismatch")

    return raw
