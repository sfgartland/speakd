"""Arbitration: rules, never inference.

A foreground channel is read in full. A background channel emits only what its
profile marks as worth interrupting for, announced with its label so you know
which of three sessions just spoke. An agent changes its own standing by
sending a control verb — the daemon obeys and does not deliberate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from speakd.channels import Channel
from speakd.model import Role


@dataclass(frozen=True)
class SpeechRequest:
    source_id: str
    text: str
    kind: str = "response"


@dataclass(frozen=True)
class Decision:
    speak: bool
    prefix: str = ""
    reason: str = ""


def decide(
    request: SpeechRequest,
    channel: Channel,
    interrupt_on: Sequence[str],
) -> Decision:
    """Whether this request is spoken, and what to announce it with."""
    if not request.text.strip():
        return Decision(speak=False, reason="empty text")
    if channel.role is Role.FOREGROUND:
        return Decision(speak=True)
    if request.kind in interrupt_on:
        prefix = f"{channel.label}:" if channel.label else ""
        return Decision(speak=True, prefix=prefix)
    return Decision(
        speak=False,
        reason=f"background channel does not interrupt for {request.kind!r}",
    )
