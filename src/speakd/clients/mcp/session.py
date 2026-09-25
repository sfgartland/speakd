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

from speakd.clients import registry

# Processes that stand between an agent and what it starts without being the
# agent: interpreters, and the shells and launchers that start them. `node` is
# not one of them -- Claude Code installed from npm runs as `node`, and is the
# agent.
_LAUNCHERS = frozenset(
    {"python", "python3", "uv", "uvx", "sh", "bash", "zsh", "dash", "fish", "env", "timeout"}
)


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


def agent_pid(chain: list[tuple[int, str]]) -> int | None:
    """The agent a process was started by: its nearest ancestor that is not a launcher.

    The one rule both sides use -- the prompt hook to record its session's
    process, this server to find it -- so they meet at the same pid however
    the agent names itself (`claude`, or `node` from npm) and however it runs
    what it starts (directly, or through `sh -c`). A looser match on any
    shared ancestor would be wrong: two agents in one terminal share the
    terminal.
    """
    for pid, comm in chain[1:]:
        if comm not in _LAUNCHERS:
            return pid
    return None


def resolve(
    client_name: str, *, proc: Path = Path("/proc"), cwd: str | None = None
) -> tuple[str, str | None]:
    """(source_id, label) for this server. A label of None means the channel has one."""
    chain = ancestors(proc)
    # The newest registration for each Claude process: `/clear` and `/resume`
    # start a new session in the same process, and the old one stays live in
    # the registry until it idles out.
    sessions: dict[int, str] = {}
    for reg in sorted(registry.live(), key=lambda r: r.touched):
        if reg.agent_pid:
            sessions[reg.agent_pid] = reg.session_id
    agent = agent_pid(chain)
    if agent is not None and agent in sessions:
        return f"claude-code:{sessions[agent]}", None
    # Not a registered Claude Code session: a channel of its own, named for
    # the agent's pid so two agents in one directory stay apart.
    if agent is None:
        agent = chain[1][0] if len(chain) > 1 else os.getppid()
    directory = Path(cwd or os.getcwd()).name or "/"
    return f"agent:{client_name}:{agent}", f"{client_name} · {directory}"
