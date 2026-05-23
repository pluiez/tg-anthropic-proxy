from shared.framing import (
    MAX_TEXT_FRAME_CHARS,
    chunk_bytes,
    coerce_text_frame_chars,
    chunk_request_envelope,
    decode_request_blob,
    make_frame,
    make_request_blob,
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
