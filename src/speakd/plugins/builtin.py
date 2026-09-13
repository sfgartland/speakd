"""Registers the transforms that ship with the core.

They are plugins like any other — the core has no privileged transform. That
is what keeps a domain pack and a built-in on equal footing.
"""

from __future__ import annotations

from speakd.plugins.host import PluginHost
from speakd.transforms.markdown import markdown
from speakd.transforms.pronunciation import pronunciation


def register_builtins(host: PluginHost) -> None:
    host.register("markdown", lambda ctx: ctx.transform("markdown", markdown))
    host.register("pronunciation", lambda ctx: ctx.transform("pronunciation", pronunciation))
