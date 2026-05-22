import base64
import gzip
import json
import uuid
from typing import Optional

# raw bytes per frame. gzip + base64 + framing JSON 之后单条 Telegram 消息
# 必须 < 4096 字符 (Bot API 文本上限). 即便 gzip 完全压缩不动 (高熵 payload),
# 2400 raw bytes 经 gzip overhead (~10B) + base64 (4/3) ≈ 3214 字符,
# 加 framing JSON ~80 字符仍稳稳塞下.
MAX_RAW_CHUNK = 2400


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
