"""In-process transform tier.

Transforms run on the critical path to first audio, so they must be fast.
Each declares a scope:

  piece — applied as pieces flow, so the job starts speaking as soon as the
  first piece clears the chain;

  job — gates the whole job. The summariser is job-scoped: a summary cannot be
  spoken before it exists.

A transform that raises is skipped and its input passes through unchanged.
Speech never disappears silently.

A transform is a plain function, registered through `PluginContext.transform`
and carried by `RegisteredTransform` (name, fn, scope, plugin). This module
used to also declare a class-shaped `Transform` Protocol -- name, scope,
apply() -- that nothing ever implemented; two shapes for one thing, one of
them a fiction. Only `Scope` survives it, and it now types the registered
transform's scope so a bad one is a type error and not only a runtime check.
"""

from __future__ import annotations

from typing import Literal

Scope = Literal["piece", "job"]
