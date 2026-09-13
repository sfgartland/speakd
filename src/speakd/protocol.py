"""Control verbs and their payloads.

The transport is deliberately not specified here: the same verbs are served
over a Unix socket and over local HTTP, so existing clients that expect a
REST-ish TTS server can drive the daemon unchanged.
"""

from __future__ import annotations

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
    SUBSCRIBE = "subscribe"
