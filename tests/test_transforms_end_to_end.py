"""The whole path: profile to transforms to segments to spoken audio."""

from collections.abc import Sequence

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import DEFAULT_PROFILE, Profile, resolve_chain
from speakd.synth.fake import FakeEngine
from speakd.transforms.chain import apply_chain

RESPONSE = """## Results

The API returned `null`.

```python
x = 1
```

- first finding
- second finding
"""


def test_a_markdown_response_becomes_speakable_segments() -> None:
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    chain, missing = resolve_chain(DEFAULT_PROFILE, host)
    assert missing == []

    source = [Piece(span=Span(0, len(RESPONSE)), spoken=RESPONSE)]
    transformed = apply_chain(source, chain)
    assert transformed.errors == []

    player = RecordingPlayer()
    result = speak(transformed.pieces, FakeEngine(), player, voice=DEFAULT_PROFILE.voice)

    spoken = " ".join(s.text for s in result.timeline.segments)
    assert "A P I" in spoken
    assert "Code block omitted." in spoken
    assert "```" not in spoken and "##" not in spoken
    assert len(player.played) == len(result.timeline)
    assert result.errors == []


def test_every_segment_keeps_a_span_inside_the_source() -> None:
    # This holds trivially: `markdown` and `pronunciation` both mark their
    # output `exact=False` (they rewrite the text), so `segment()` always
    # stamps every unit with the *parent* piece's span verbatim -- here that
    # is always exactly Span(0, len(RESPONSE)), the span this test itself
    # constructs. The assertion below is true for any text these transforms
    # could produce, including wrong output, so it documents the containment
    # property rather than guarding it. See
    # test_an_exact_chain_produces_real_sub_spans_inside_the_source below for
    # a chain that actually exercises the span arithmetic.
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    chain, _ = resolve_chain(DEFAULT_PROFILE, host)
    source = [Piece(span=Span(0, len(RESPONSE)), spoken=RESPONSE)]
    transformed = apply_chain(source, chain)
    result = speak(transformed.pieces, FakeEngine(), RecordingPlayer())
    for segment in result.timeline.segments:
        assert 0 <= segment.span.start <= segment.span.end <= len(RESPONSE)


def _identity(pieces: Sequence[Piece]) -> list[Piece]:
    """A transform that changes nothing, so `exact` survives at its default."""
    return list(pieces)


def test_an_exact_chain_produces_real_sub_spans_inside_the_source() -> None:
    # Companion to the test above, which can't fail: neither built-in
    # transform ever leaves `exact=True`, so its containment check is true
    # by construction. Here the chain is a single identity transform that
    # leaves each piece's `exact` flag at its default (True), so `segment()`
    # computes genuine sub-spans from sentence-boundary offsets into the real
    # source text -- and the containment assertion can actually observe a
    # wrong one.
    host = PluginHost(ServiceRegistry())
    host.register("identity", lambda ctx: ctx.transform("identity", _identity))
    profile = Profile(name="identity-only", transforms=("identity",))
    chain, missing = resolve_chain(profile, host)
    assert missing == []

    source = [Piece(span=Span(0, len(RESPONSE)), spoken=RESPONSE)]
    transformed = apply_chain(source, chain)
    assert transformed.errors == []

    result = speak(transformed.pieces, FakeEngine(), RecordingPlayer())
    segments = result.timeline.segments
    assert len(segments) > 1
    spans = {(s.span.start, s.span.end) for s in segments}
    assert len(spans) > 1, "sub-spans should differ, not all collapse to the parent span"
    for segment in segments:
        assert 0 <= segment.span.start <= segment.span.end <= len(RESPONSE)
        assert RESPONSE[segment.span.start : segment.span.end].strip() == segment.text
