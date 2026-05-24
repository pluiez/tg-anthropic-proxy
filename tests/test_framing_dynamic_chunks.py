import random

from shared.framing import (
    MAX_TEXT_FRAME_CHARS,
    chunk_bytes,
    chunk_bytes_for_frame_payloads,
    coerce_text_frame_chars,
    chunk_request_envelope,
    decode_request_blob,
    make_frame,
    make_request_blob,
    parse_frame,
    parse_request_blob_caption,
)


def test_chunk_request_envelope_packs_compressible_payloads_more_efficiently() -> None:
    rid = "r_test123456"
    data = (
        b'{"model":"claude-opus-4-7","messages":['
        + b'{"role":"user","content":"hello world"},' * 3000
        + b']}\n'
    )

    chunks = chunk_request_envelope(data, rid)

    assert b"".join(chunks) == data
    assert len(chunks) < len(chunk_bytes(data))
    total = len(chunks)
    assert all(
        len(make_frame(rid, seq, "req", data=chunk, total=total))
        <= MAX_TEXT_FRAME_CHARS
        for seq, chunk in enumerate(chunks)
    )


def test_chunk_request_envelope_keeps_high_entropy_payloads_under_frame_limit() -> None:
    rid = "r_test123456"
    data = bytes(range(256)) * 200

    chunks = chunk_request_envelope(data, rid)

    assert b"".join(chunks) == data
    total = len(chunks)
    assert all(
        len(make_frame(rid, seq, "req", data=chunk, total=total))
        <= MAX_TEXT_FRAME_CHARS
        for seq, chunk in enumerate(chunks)
    )


def test_coerce_text_frame_chars_uses_hermes_like_default_and_clamps() -> None:
    assert coerce_text_frame_chars(None) == MAX_TEXT_FRAME_CHARS
    assert coerce_text_frame_chars("4000") == 4000
    assert coerce_text_frame_chars(4000) == 4000
    assert coerce_text_frame_chars("99999") == 4096
    assert coerce_text_frame_chars("12") == 1024
    assert coerce_text_frame_chars("not-an-int") == MAX_TEXT_FRAME_CHARS


def test_request_blob_round_trips_and_uses_small_caption() -> None:
    rid = "r_test123456"
    envelope = b'{"path":"/v1/messages","body":"hello"}' * 1000

    caption, blob = make_request_blob(rid, envelope)
    metadata = parse_request_blob_caption(caption)

    assert metadata is not None
    assert metadata["rid"] == rid
    assert len(caption) <= 1024
    assert decode_request_blob(blob, metadata) == envelope


def test_request_blob_rejects_corrupted_payload() -> None:
    caption, blob = make_request_blob("r_test123456", b"hello")
    metadata = parse_request_blob_caption(caption)

    assert metadata is not None
    try:
        decode_request_blob(blob + b"bad", metadata)
    except ValueError as exc:
        assert "gzip" in str(exc) or "sha256" in str(exc)
    else:
        raise AssertionError("corrupted request blob should be rejected")


def test_dynamic_payload_chunks_respect_nonzero_start_seq() -> None:
    rid = "r_test123456"
    data = (b"event: content_block_delta\n" + b"data: {\"text\":\"hello world\"}\n\n") * 500
    start_seq = 42

    chunks = chunk_bytes_for_frame_payloads(
        data,
        rid,
        "resp_chunk",
        max_chars=MAX_TEXT_FRAME_CHARS,
        start_seq=start_seq,
    )

    assert b"".join(chunks) == data
    assert len(chunks) < len(chunk_bytes(data))
    assert all(
        len(make_frame(rid, start_seq + offset, "resp_chunk", data=chunk))
        <= MAX_TEXT_FRAME_CHARS
        for offset, chunk in enumerate(chunks)
    )


def test_dynamic_payload_chunks_count_last_extra_only_on_final_frame() -> None:
    rid = "r_test123456"
    rng = random.Random(0)
    data = bytes(rng.randrange(256) for _ in range(20_000))
    start_seq = 7

    chunks = chunk_bytes_for_frame_payloads(
        data,
        rid,
        "resp_chunk",
        max_chars=1024,
        last_extra={"eof": True},
        start_seq=start_seq,
    )

    assert len(chunks) > 1
    assert b"".join(chunks) == data

    frames = [
        make_frame(
            rid,
            start_seq + offset,
            "resp_chunk",
            data=chunk,
            **({"eof": True} if offset == len(chunks) - 1 else {}),
        )
        for offset, chunk in enumerate(chunks)
    ]
    parsed = [parse_frame(frame) for frame in frames]

    assert all(len(frame) <= 1024 for frame in frames)
    assert all(frame is not None for frame in parsed)
    assert [frame.get("eof") for frame in parsed[:-1] if frame is not None] == [None] * (len(chunks) - 1)
    assert parsed[-1] is not None
    assert parsed[-1]["eof"] is True
