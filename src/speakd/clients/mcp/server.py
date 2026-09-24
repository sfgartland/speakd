"""speakd-mcp: an MCP server an agent briefs the user through.

The agent doing the work is the one that knows what is worth saying -- that it
finished, that it is stuck, that it needs an answer -- so it is the agent that
decides, and this is how it says it. The daemon stays what it was: a queue
and a voice.

MCP over stdio, one JSON-RPC message per line. Hand-written rather than built
on the MCP SDK: the subset an agent needs is five methods, and the SDK would
bring pydantic, anyio and httpx into a project that has kept its dependencies
to what speaking needs.

Nothing here raises into the agent. A missing daemon is an answer (`spoken:
false`), a bad call is a tool error, a bad line is a JSON-RPC error; the
server goes on serving through all three, and ends when its stdin does.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from speakd.clients.mcp import guide, session
from speakd.clients.send import call
from speakd.protocol import Request, Response, Verb

PROTOCOL_VERSION = "2025-06-18"

PREAMBLE = (
    "speakd reads to the user aloud. Use `brief` to tell them what matters, "
    "`briefing_status` to see how this session is set, and `set_mode` when "
    'they ask to hear every response ("full") or only briefings ("brief"). '
    "In full mode every response is already read aloud, so do not brief then."
)

KINDS = ("done", "problem", "question", "progress")

TOOLS = [
    {
        "name": "brief",
        "description": (
            "Tell the user something by voice: that you finished (with the gist), a problem, "
            "a question for them, or progress on a long task. One or two short sentences."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to say, as spoken words."},
                "kind": {"type": "string", "enum": list(KINDS)},
            },
            "required": ["text", "kind"],
        },
    },
    {
        "name": "briefing_status",
        "description": (
            "This session's voice settings: mode (brief or full), whether it is muted, "
            "and the user's standing guide for what to tell them."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_mode",
        "description": (
            "Switch this session between brief (only what you choose to say) and full "
            "(every response read aloud), when the user asks for it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["brief", "full"]}},
            "required": ["mode"],
        },
    },
]

_NOT_RUNNING = "speakd is not running"


class ToolError(Exception):
    """A call the agent got wrong: reported to it as a tool error, not raised."""


class Server:
    def __init__(self, socket: Path | None, proc: Path = Path("/proc")) -> None:
        self._socket = socket
        self._proc = proc
        self._client = "agent"
        self._channel: str | None = None
        self._label: str | None = None

    # ---- JSON-RPC ----

    def handle(self, message: object) -> dict[str, object] | None:
        """Answer one message, or None for a notification.

        A message with no `id` is a notification, and JSON-RPC answers none --
        not even with an error: a reply with nothing to match it to is a
        stray line a strict client may reject.
        """
        if not isinstance(message, dict):
            return _error(None, -32600, "invalid request")
        if "id" not in message:
            return None
        if not isinstance(message.get("method"), str):
            return _error(message.get("id"), -32600, "invalid request")
        method: str = message["method"]
        ident = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            info = params.get("clientInfo")
            if isinstance(info, dict) and isinstance(info.get("name"), str):
                self._client = info["name"]
            version = params.get("protocolVersion")
            return _result(
                ident,
                {
                    "protocolVersion": version if isinstance(version, str) else PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "speakd", "version": "0.1"},
                    "instructions": PREAMBLE + "\n\n" + guide.text(),
                },
            )
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": TOOLS})
        if method == "tools/call":
            return _result(ident, self._call_tool(params))
        return _error(ident, -32601, f"method not found: {method}")

    def _call_tool(self, params: dict[str, object]) -> dict[str, object]:
        name = params.get("name")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            if name in ("brief", "briefing_status", "set_mode"):
                self._arrive()
            if name == "brief":
                data = self._brief(arguments)
            elif name == "briefing_status":
                data = self._status()
            elif name == "set_mode":
                data = self._set_mode(arguments)
            else:
                raise ToolError(f"unknown tool: {name}")
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {
            "content": [{"type": "text", "text": _summary(data)}],
            "structuredContent": data,
            "isError": False,
        }

    # ---- tools ----

    def _brief(self, arguments: dict[str, object]) -> dict[str, object]:
        text = arguments.get("text")
        kind = arguments.get("kind")
        if not isinstance(text, str):
            raise ToolError("brief needs 'text': what to say")
        if kind not in KINDS:
            raise ToolError(f"brief needs 'kind': one of {', '.join(KINDS)}")
        if not text.strip():
            return {"spoken": False, "reason": "nothing to say"}
        answer = self._send(Verb.ENQUEUE, {"text": text, "kind": "brief", "brief_kind": kind})
        if isinstance(answer, str):
            return {"spoken": False, "reason": _NOT_RUNNING}
        if not answer.ok:
            return {"spoken": False, "reason": answer.error}
        data: dict[str, object] = {"spoken": bool(answer.data.get("spoken"))}
        if not data["spoken"]:
            data["reason"] = answer.data.get("reason", "not spoken")
        data.update(self._settings())
        return data

    def _status(self) -> dict[str, object]:
        data = self._settings()
        data["guide"] = guide.text()
        return data

    def _set_mode(self, arguments: dict[str, object]) -> dict[str, object]:
        mode = arguments.get("mode")
        if mode not in ("brief", "full"):
            raise ToolError("set_mode needs 'mode': 'brief' or 'full'")
        answer = self._send(Verb.SET_MODE, {"mode": mode})
        if isinstance(answer, str):
            return {"reason": _NOT_RUNNING}
        if not answer.ok:
            raise ToolError(answer.error)
        return self._status()

    # ---- the daemon ----

    def _settings(self) -> dict[str, object]:
        """This channel's mode, mute and capability, as the daemon has them."""
        answer = self._send(Verb.STATUS, {}, source="")
        if isinstance(answer, str) or not answer.ok:
            return {"reason": _NOT_RUNNING}
        mine = self._channel_id()
        channels = answer.data.get("channels")
        for channel in channels if isinstance(channels, list) else []:
            if isinstance(channel, dict) and channel.get("source_id") == mine:
                muted = bool(channel.get("muted")) or bool(answer.data.get("muted"))
                return {
                    "mode": channel.get("mode", "full"),
                    "muted": muted,
                    "briefs": bool(channel.get("briefs")),
                }
        # Not listed yet: nothing has opened the channel. Said as unknown
        # rather than guessed, since the agent acts on what this says.
        return {"mode": "unknown", "muted": bool(answer.data.get("muted")), "briefs": False}

    def _channel_id(self) -> str:
        """The channel this call briefs on, settled by `_arrive`."""
        if self._channel is None:
            self._arrive()
        assert self._channel is not None
        return self._channel

    def _arrive(self) -> None:
        """Work out which channel this is, and tell the daemon a briefer is on it.

        Done at the start of every tool call rather than once. The session can
        change under a running server -- `/clear` starts a new one in the same
        Claude process -- and the daemon can forget: a restart brings a
        channel back with no briefer, where it reads as full mode and every
        briefing would be refused as one, telling the agent to stop. Both are
        a directory listing and two local round trips; a briefing is rare.
        """
        channel, label = session.resolve(self._client, proc=self._proc)
        self._channel, self._label = channel, label
        call(Request(Verb.SET_CAPABILITIES, channel, {"briefs": True}), socket=self._socket)
        if label:
            call(Request(Verb.SET_LABEL, channel, {"label": label}), socket=self._socket)

    def _send(
        self, verb: Verb, payload: dict[str, object], source: str | None = None
    ) -> Response | str:
        return call(
            Request(verb, self._channel_id() if source is None else source, payload),
            socket=self._socket,
        )


def _result(ident: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _summary(data: dict[str, object]) -> str:
    """One line for agents that read text content rather than structured content."""
    parts = [f"{key}: {value}" for key, value in data.items() if key != "guide"]
    if "guide" in data:
        parts.append(f"guide:\n{data['guide']}")
    return "; ".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="speakd-mcp", description=__doc__)
    parser.add_argument("--socket", type=Path, default=None, help="where speakd listens")
    args = parser.parse_args(argv)
    server = Server(args.socket)
    # Bytes, decoded here, rather than text stdin: a line that is not UTF-8
    # would otherwise raise out of the loop and end the server, and with it
    # the agent's voice for the rest of the session. Replaced characters then
    # fail as JSON, which is the error such a line deserves.
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace")
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, object] | None = _error(None, -32700, "parse error")
        else:
            reply = server.handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
