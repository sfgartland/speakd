"""Tests for the wire protocol."""

import pytest

from speakd.protocol import (
    ProtocolError,
    Request,
    Response,
    Verb,
    decode_request,
    decode_response,
    encode,
)


def test_a_request_round_trips() -> None:
    request = Request(verb=Verb.ENQUEUE, source_id="session:abc", payload={"text": "hello"})
    assert decode_request(encode(request)) == request


def test_a_response_round_trips() -> None:
    response = Response(ok=True, data={"queued": 1})
    assert decode_response(encode(response)) == response


def test_an_error_response_round_trips() -> None:
    response = Response(ok=False, data={}, error="unknown source")
    decoded = decode_response(encode(response))
    assert decoded.ok is False
    assert decoded.error == "unknown source"


def test_encoding_ends_with_exactly_one_newline() -> None:
    blob = encode(Request(verb=Verb.HUSH, source_id="s", payload={}))
    assert blob.endswith(b"\n")
    assert blob.count(b"\n") == 1


def test_payload_defaults_to_empty() -> None:
    decoded = decode_request(b'{"verb": "hush", "source_id": "s"}\n')
    assert decoded.payload == {}


def test_an_unknown_verb_is_a_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="verb"):
        decode_request(b'{"verb": "explode", "source_id": "s"}\n')


def test_a_missing_verb_is_a_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="verb"):
        decode_request(b'{"source_id": "s"}\n')


def test_a_missing_source_id_is_a_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="source_id"):
        decode_request(b'{"verb": "hush"}\n')


def test_malformed_json_is_a_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="JSON"):
        decode_request(b"{not json\n")


def test_a_non_object_message_is_a_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="object"):
        decode_request(b"[1, 2, 3]\n")


def test_a_payload_that_is_not_an_object_is_a_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="payload"):
        decode_request(b'{"verb": "hush", "source_id": "s", "payload": 7}\n')


def test_every_verb_survives_a_round_trip() -> None:
    for verb in Verb:
        request = Request(verb=verb, source_id="s", payload={})
        assert decode_request(encode(request)).verb is verb
