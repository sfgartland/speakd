"""Tests for plugin activation, deactivation and registration cleanup."""

from collections.abc import Sequence

from speakd.model import Piece
from speakd.plugins.host import PluginContext, PluginHost
from speakd.plugins.registry import ServiceRegistry


def upper(pieces: Sequence[Piece]) -> Sequence[Piece]:
    return [Piece(span=p.span, spoken=p.spoken.upper(), exact=False) for p in pieces]


def test_a_plugin_with_no_requirements_activates_immediately() -> None:
    host = PluginHost(ServiceRegistry())
    host.register("shouty", lambda ctx: ctx.transform("upper", upper))
    assert host.active() == {"shouty"}
    assert [t.name for t in host.transforms()] == ["upper"]


def test_a_plugin_waits_for_its_required_service() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    assert host.active() == set()
    assert host.transforms() == []

    registry.provide("bibliography", {"Stiegler-1994": "Stiegler, 1994"})
    assert host.active() == {"citations"}
    assert [t.name for t in host.transforms()] == ["cite"]


def test_a_plugin_deactivates_when_its_provider_disappears() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    handle = registry.provide("bibliography", {})
    handle.dispose()
    assert host.active() == set()
    assert host.transforms() == []


def test_a_plugin_reactivates_when_its_provider_returns() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    registry.provide("bibliography", {}).dispose()
    registry.provide("bibliography", {})
    assert host.active() == {"citations"}


def test_require_hands_the_service_to_the_plugin() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    seen: list[object] = []
    host.register(
        "p", lambda ctx: seen.append(ctx.require("bibliography")), requires=["bibliography"]
    )
    registry.provide("bibliography", "the-bib")
    assert seen == ["the-bib"]


def test_unregister_disposes_everything_the_plugin_registered() -> None:
    host = PluginHost(ServiceRegistry())
    torn_down: list[str] = []

    def setup(ctx: PluginContext) -> None:
        ctx.transform("upper", upper)
        ctx.on_dispose(lambda: torn_down.append("yes"))

    host.register("shouty", setup)
    host.unregister("shouty")
    assert host.transforms() == []
    assert host.active() == set()
    assert torn_down == ["yes"]


def test_a_plugin_can_provide_a_service_for_another() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("vault", lambda ctx: ctx.provide("bibliography", "the-bib"))
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    assert host.active() == {"vault", "citations"}
    host.unregister("vault")
    assert host.active() == set()


def test_a_plugin_whose_setup_raises_does_not_activate() -> None:
    host = PluginHost(ServiceRegistry())

    def boom(ctx: PluginContext) -> None:
        ctx.transform("half", upper)
        raise RuntimeError("setup failed")

    host.register("broken", boom)
    assert host.active() == set()
    assert host.transforms() == []
    assert any("setup failed" in e for e in host.errors())


def test_transform_scope_defaults_to_piece_and_can_be_job() -> None:
    host = PluginHost(ServiceRegistry())
    host.register("p", lambda ctx: ctx.transform("summarize", upper, scope="job"))
    assert host.transforms()[0].scope == "job"
