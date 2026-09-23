"""Control verbs and their payloads.

The transport is deliberately not specified here: the same verbs are served
over a Unix socket and over local HTTP, so existing clients that expect a
REST-ish TTS server can drive the daemon unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class Verb(str, Enum):
    ENQUEUE = "enqueue"
    CANCEL = "cancel"
    HUSH = "hush"
    PAUSE = "pause"
    RESUME = "resume"
    SEEK = "seek"
    SET_ROLE = "set_role"
    SET_PRIORITY = "set_priority"
    SET_LABEL = "set_label"
    MUTE = "mute"
    SET_ENGINE = "set_engine"
    SET_SPEED = "set_speed"
    SUBSCRIBE = "subscribe"
    STATUS = "status"


class ProtocolError(Exception):
    """A message that could not be understood, with what was wrong."""


@dataclass(frozen=True)
class Request:
    verb: Verb
    source_id: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    ok: bool
    data: dict[str, object] = field(default_factory=dict)
    error: str = ""


def encode(message: Request | Response) -> bytes:
    """Serialise one message as a single JSON line."""
    if isinstance(message, Request):
        body: dict[str, object] = {
            "verb": message.verb.value,
            "source_id": message.source_id,
            "payload": message.payload,
        }
    else:
        body = {"ok": message.ok, "data": message.data, "error": message.error}
    return (json.dumps(body) + "\n").encode("utf-8")


def _loads(line: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"could not parse JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("message must be a JSON object")
    return parsed


def decode_request(line: bytes) -> Request:
    body = _loads(line)
    raw_verb = body.get("verb")
    if not isinstance(raw_verb, str):
        raise ProtocolError("message is missing a verb")
    try:
        verb = Verb(raw_verb)
    except ValueError as exc:
        raise ProtocolError(f"unknown verb {raw_verb!r}") from exc
    source_id = body.get("source_id")
    if not isinstance(source_id, str):
        raise ProtocolError("message is missing a source_id")
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be a JSON object")
    return Request(verb=verb, source_id=source_id, payload=payload)


def decode_response(line: bytes) -> Response:
    body = _loads(line)
    ok = body.get("ok")
    if not isinstance(ok, bool):
        raise ProtocolError("response is missing ok")
    data = body.get("data", {})
    if not isinstance(data, dict):
        raise ProtocolError("data must be a JSON object")
    error = body.get("error", "")
    if not isinstance(error, str):
        raise ProtocolError("error must be a string")
    return Response(ok=ok, data=data, error=error)
