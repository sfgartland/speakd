"""Which channel an MCP server briefs on.

An MCP server is started by the agent, with nothing on its command line to
say which session it serves. For Claude Code the answer is in the process
tree: the prompt hook and this server are both children of the same `claude`
process, and the hook records that process's pid in the session registry.
Anything else -- Codex, OpenCode -- gets a channel named for the agent and the
nearest process that is the agent itself, labelled with its directory.

Stdlib and the registry only: the hook imports `ancestors` from here, and the
hook is on the user's critical path.
"""

from __future__ import annotations

import os
from pathlib import Path

from speakd.clients.claude_code import registry

# Processes that stand between an agent and this server without being the
# agent: interpreters and the shells and launchers that start them.
_LAUNCHERS = frozenset({"python", "python3", "uv", "uvx", "sh", "bash", "zsh", "node", "env"})


def ancestors(
    proc: Path = Path("/proc"), start: str = "self", limit: int = 8
) -> list[tuple[int, str]]:
    """(pid, command name) for `start` and its ancestors, nearest first. Never raises."""
    found: list[tuple[int, str]] = []
    current = start
    for _ in range(limit):
        try:
            stat = (proc / current / "stat").read_text()
            comm = (proc / current / "comm").read_text().strip()
        except OSError:
            break
        # The command name is in parentheses and may itself contain spaces
        # or parentheses; everything after the last ")" is space-separated.
        head, _, tail = stat.rpartition(")")
        fields = tail.split()
        try:
            pid = int(head.split("(", 1)[0])
            parent = int(fields[1])
        except (ValueError, IndexError):
            break
        found.append((pid, comm))
        if parent <= 1:
            break
        current = str(parent)
    return found


def resolve(
    client_name: str, *, proc: Path = Path("/proc"), cwd: str | None = None
) -> tuple[str, str | None]:
    """(source_id, label) for this server. A label of None means the channel has one."""
    chain = ancestors(proc)
    sessions = {r.claude_pid: r.session_id for r in registry.live() if r.claude_pid}
    for pid, _comm in chain:
        if pid in sessions:
            return f"claude-code:{sessions[pid]}", None
    # Not a registered Claude Code session: the nearest ancestor that is not a
    # launcher is the agent, and its pid keeps two agents in one directory apart.
    parents = chain[1:]
    agent = next((pid for pid, comm in parents if comm not in _LAUNCHERS), None)
    if agent is None:
        agent = parents[0][0] if parents else os.getppid()
    directory = Path(cwd or os.getcwd()).name or "/"
    return f"agent:{client_name}:{agent}", f"{client_name} · {directory}"
