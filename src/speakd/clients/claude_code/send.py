"""One request to the daemon, and never an exception.

A hook that raises fails the user's turn, so every failure here leaves as a
string the caller can log: no daemon, a timeout, a socket that went away
mid-request. The daemon not running is the normal case, not an error — the
user may simply not want speech today.
"""

from __future__ import annotations

from pathlib import Path

from speakd.cli import default_socket_path
from speakd.protocol import Request, Verb

# Claude Code gives a hook a bounded window (three to five seconds in the
# manifest this client ships). Two seconds is generous for a local socket
# round trip and short enough that a wedged daemon never costs the turn.
TIMEOUT = 2.0


def send(request: Request, *, socket: Path | None = None, timeout: float = TIMEOUT) -> str | None:
    """Send one request. Return `None` on success, or a one-line reason."""
    address = socket if socket is not None else default_socket_path()
    try:
        from speakd.transport import connect

        client = connect(address)
    except OSError as exc:
        return f"no daemon at {address} ({exc.strerror or exc})"
    except Exception as exc:  # pragma: no cover - defence, not a path
        return f"could not reach {address}: {exc}"

    try:
        response = client.send(request, timeout=timeout)
    except Exception as exc:
        return f"{request.verb.value} failed: {exc}"
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass

    if not response.ok:
        return f"{request.verb.value} refused: {response.error or 'no reason given'}"
    return None


def enqueue(
    source: str,
    text: str,
    *,
    socket: Path | None = None,
    timeout: float = TIMEOUT,
    priority: int | None = None,
) -> str | None:
    """Speak `text` on `source`. Blank text is a no-op, not a request.

    `priority` rides along in the payload. Today's daemon reads priority only
    from `set_priority` and ignores it here, so sending it changes nothing —
    it is on the wire so that a daemon which grows per-utterance priority
    finds this client already speaking its language.
    """
    if not text.strip():
        return None
    payload: dict[str, object] = {"text": text}
    if priority is not None:
        payload["priority"] = priority
    return send(
        Request(verb=Verb.ENQUEUE, source_id=source, payload=payload),
        socket=socket,
        timeout=timeout,
    )


def hush(source: str, *, socket: Path | None = None, timeout: float = TIMEOUT) -> str | None:
    """Stop `source` talking and drop what it had queued."""
    return send(Request(verb=Verb.HUSH, source_id=source), socket=socket, timeout=timeout)
