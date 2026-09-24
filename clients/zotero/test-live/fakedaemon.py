"""Throwaway: this branch's daemon, socket and HTTP transport, with a fake engine
and a sink that takes real time. For live-testing the Zotero plugin."""

import sys
import time
from pathlib import Path

import numpy as np

from speakd.__main__ import build_profiles, serve
from speakd.channels import ChannelTable
from speakd.daemon import Daemon
from speakd.events import EventBus
from speakd.player import FakeSink, StreamingPlayer
from speakd.synth.fake import FakeEngine


class RealTimeSink(FakeSink):
    def write(self, frames: np.ndarray) -> None:
        super().write(frames)
        time.sleep(len(frames) / 24000)


d = Daemon(
    FakeEngine(chars_per_second=15.0, synthesis_cost=0.2),
    StreamingPlayer(RealTimeSink()),
    build_profiles(),
    bus=EventBus(),
    channels=ChannelTable(),
)
sys.exit(serve(d, Path(sys.argv[1])))
