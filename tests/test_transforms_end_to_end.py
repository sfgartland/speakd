"""The whole path: profile to transforms to segments to spoken audio."""

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import DEFAULT_PROFILE, resolve_chain
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
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    chain, _ = resolve_chain(DEFAULT_PROFILE, host)
    source = [Piece(span=Span(0, len(RESPONSE)), spoken=RESPONSE)]
    transformed = apply_chain(source, chain)
    result = speak(transformed.pieces, FakeEngine(), RecordingPlayer())
    for segment in result.timeline.segments:
        assert 0 <= segment.span.start <= segment.span.end <= len(RESPONSE)
