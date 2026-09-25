"""Tests for A4: the daemon's render verbs, events, status and settings.

`render.py` (A1-A3) already has `RenderQueue`; this is about the daemon
owning one, wiring it to its own engine/lock/`speaking`, and exposing it
over the protocol. Uses `FakeEngine` throughout -- no ffmpeg needed for any
test here, since none of these render to completion (a running/paused state
is all the verbs and events need to prove).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import FakeSink, StreamingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.render import RenderJob, RenderQueue, ffmpeg_available, save_manifest
from speakd.synth.fake import FakeEngine


def until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


def make_daemon(*, engine: FakeEngine | None = None) -> Daemon:
    return Daemon(
        engine if engine is not None else FakeEngine(),
        StreamingPlayer(FakeSink()),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )


def render_payload(tmp_path: Path, **overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = dict(
        parts=[{"title": "Whole", "text": "One. Two. Three."}],
        out=str(tmp_path / "out.mp3"),
        format="mp3",
        lang="en",
        profile="default",
        metadata={"title": "T", "artist": "A", "album": "B", "date": "2026"},
    )
    defaults.update(overrides)
    return defaults


def job_id_of(response: Response) -> str:
    job_id = response.data["job"]
    assert isinstance(job_id, str)
    return job_id


# --- the daemon owns a RenderQueue, wired to its own engine/lock/speaking ---


def test_daemon_owns_a_render_queue() -> None:
    d = make_daemon()
    assert isinstance(d.render_queue, RenderQueue)


def test_start_resumes_unfinished_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "speakd-state"
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(state_dir))
    work_root = state_dir / "renders"
    job = RenderJob(
        id="resume-me",
        parts=[],
        out=tmp_path / "out.mp3",
        format="mp3",
        lang="en",
        voice="af_heart",
        state="running",
    )
    save_manifest(job, work_root / job.id)

    d = make_daemon()
    d.start()
    try:
        assert until(lambda: d.render_queue.job("resume-me") is not None)
        resumed = d.render_queue.job("resume-me")
        assert resumed is not None
        assert resumed.state in ("queued", "running", "done", "failed")
    finally:
        d.stop()


def test_stop_stops_the_render_queue_cleanly() -> None:
    d = make_daemon()
    d.start()
    thread = d.render_queue._thread
    d.stop()
    assert until(lambda: not thread.is_alive())


# --- the render verb -----------------------------------------------------


def test_render_verb_answers_job_and_enqueues_it(tmp_path: Path) -> None:
    d = make_daemon(engine=FakeEngine(synthesis_cost=0.05))
    d.start()
    try:
        response = d.handle(
            Request(verb=Verb.RENDER, source_id="zotero:1", payload=render_payload(tmp_path))
        )
        assert response.ok
        job_id = job_id_of(response)
        assert job_id
        assert until(lambda: d.render_queue.job(job_id) is not None)
    finally:
        d.stop()


def test_render_verb_refuses_missing_parts(tmp_path: Path) -> None:
    d = make_daemon()
    d.start()
    try:
        payload = render_payload(tmp_path)
        del payload["parts"]
        response = d.handle(Request(verb=Verb.RENDER, source_id="zotero:1", payload=payload))
        assert not response.ok
    finally:
        d.stop()


def test_render_verb_drops_a_trailing_whitespace_only_part(tmp_path: Path) -> None:
    """Review Focus #2: `speakctl render`'s pdftotext-split parts end with
    exactly this when the source file ends in a form feed -- the last chunk
    is empty. A part whose text is nothing but whitespace contributes no
    audio and no chapter, so it is dropped rather than carried through as
    one that will simply synthesise nothing."""
    d = make_daemon(engine=FakeEngine(synthesis_cost=0.05))
    d.start()
    try:
        payload = render_payload(tmp_path)
        payload["parts"] = [
            {"title": "Whole", "text": "One. Two. Three."},
            {"title": "Trailing", "text": "  \n"},  # pdftotext's trailing \f, split to nothing
        ]
        response = d.handle(Request(Verb.RENDER, "cli", payload))
        assert response.ok, response.error
        job = d.render_queue.job(job_id_of(response))
        assert job is not None
        assert len(job.parts) == 1
        assert job.parts[0].title == "Whole"
    finally:
        d.render_queue.stop()
        d.stop()


def test_render_verb_refuses_a_job_whose_parts_are_all_whitespace(tmp_path: Path) -> None:
    d = make_daemon()
    d.start()
    try:
        payload = render_payload(tmp_path)
        payload["parts"] = [{"title": "Whole", "text": "   \n\t"}]
        response = d.handle(Request(Verb.RENDER, "cli", payload))
        assert not response.ok
        assert "text" in response.error
    finally:
        d.stop()


def test_render_verb_refuses_unknown_format(tmp_path: Path) -> None:
    d = make_daemon()
    d.start()
    try:
        response = d.handle(
            Request(
                verb=Verb.RENDER,
                source_id="zotero:1",
                payload=render_payload(tmp_path, format="wav"),
            )
        )
        assert not response.ok
    finally:
        d.stop()


def test_render_verb_reads_render_settings_when_the_job_starts(tmp_path: Path) -> None:
    d = make_daemon(engine=FakeEngine(synthesis_cost=0.05))
    d.start()
    try:
        d.settings.set("render.mp3_bitrate", "32k")
        d.settings.set("render.chapter_gap_ms", 500)
        response = d.handle(
            Request(verb=Verb.RENDER, source_id="zotero:1", payload=render_payload(tmp_path))
        )
        assert response.ok
        job_id = job_id_of(response)
        assert until(lambda: d.render_queue.job(job_id) is not None)
        job = d.render_queue.job(job_id)
        assert job is not None
        assert job.bitrate == "32k"
        assert job.chapter_gap_ms == 500
    finally:
        d.stop()


# --- render_cancel ---------------------------------------------------------


def test_render_cancel_removes_a_queued_job_without_touching_the_running_one(
    tmp_path: Path,
) -> None:
    d = make_daemon(engine=FakeEngine(synthesis_cost=0.2))
    d.start()
    try:
        running = d.handle(
            Request(
                verb=Verb.RENDER,
                source_id="zotero:1",
                payload=render_payload(tmp_path, out=str(tmp_path / "running.mp3")),
            )
        )
        queued = d.handle(
            Request(
                verb=Verb.RENDER,
                source_id="zotero:1",
                payload=render_payload(tmp_path, out=str(tmp_path / "queued.mp3")),
            )
        )
        running_id = job_id_of(running)
        queued_id = job_id_of(queued)

        def running_state() -> str | None:
            job = d.render_queue.job(running_id)
            return job.state if job is not None else None

        def queued_state() -> str | None:
            job = d.render_queue.job(queued_id)
            return job.state if job is not None else None

        assert until(lambda: running_state() == "running")

        response = d.handle(
            Request(verb=Verb.RENDER_CANCEL, source_id="zotero:1", payload={"job": queued_id})
        )
        assert response.ok
        assert response.data["cancelled"] is True
        assert queued_state() == "cancelled"
        assert running_state() == "running"
    finally:
        d.stop()


def test_render_cancel_of_unknown_job_answers_false(tmp_path: Path) -> None:
    d = make_daemon()
    d.start()
    try:
        response = d.handle(
            Request(verb=Verb.RENDER_CANCEL, source_id="zotero:1", payload={"job": "nope"})
        )
        assert response.ok
        assert response.data["cancelled"] is False
    finally:
        d.stop()


# --- status.render ----------------------------------------------------------


def test_status_reports_render_available_and_jobs(tmp_path: Path) -> None:
    d = make_daemon(engine=FakeEngine(synthesis_cost=0.05))
    d.start()
    try:
        response = d.handle(
            Request(verb=Verb.RENDER, source_id="zotero:1", payload=render_payload(tmp_path))
        )
        job_id = job_id_of(response)
        status = d.handle(Request(verb=Verb.STATUS, source_id="zotero:1"))
        assert status.ok
        render_status = status.data["render"]
        assert isinstance(render_status, dict)
        assert render_status["available"] == ffmpeg_available()
        jobs = render_status["jobs"]
        assert isinstance(jobs, list)
        job_ids = [j["job"] for j in jobs]
        assert job_id in job_ids
    finally:
        d.stop()


# --- the render event -------------------------------------------------------


def test_render_event_fires_on_state_changes_and_is_throttled_while_running(
    tmp_path: Path,
) -> None:
    engine = FakeEngine(synthesis_cost=0.02)
    d = make_daemon(engine=engine)
    seen: list[Event] = []
    d.bus.subscribe(lambda e: seen.append(e) if e.kind == "render" else None)
    d.start()
    try:
        long_text = " ".join(f"Sentence{i}." for i in range(8))
        response = d.handle(
            Request(
                verb=Verb.RENDER,
                source_id="zotero:1",
                payload=render_payload(tmp_path, parts=[{"title": "Whole", "text": long_text}]),
            )
        )
        job_id = job_id_of(response)

        def job_state() -> str | None:
            job = d.render_queue.job(job_id)
            return job.state if job is not None else None

        assert until(lambda: job_state() == "done", timeout=10.0)

        states = [e.data["state"] for e in seen if e.data.get("job") == job_id]
        # At least queued, running and a terminal state were announced.
        assert "queued" in states or "running" in states
        assert states[-1] == "done"
        for e in seen:
            assert set(e.data) >= {
                "job",
                "state",
                "part",
                "parts",
                "done_seconds",
                "estimate_seconds",
                "out",
                "error",
            }
    finally:
        d.stop()


def test_render_event_fields_include_out_only_when_done(tmp_path: Path) -> None:
    engine = FakeEngine(synthesis_cost=0.01)
    d = make_daemon(engine=engine)
    seen: list[Event] = []
    d.bus.subscribe(lambda e: seen.append(e) if e.kind == "render" else None)
    d.start()
    try:
        response = d.handle(
            Request(verb=Verb.RENDER, source_id="zotero:1", payload=render_payload(tmp_path))
        )
        job_id = job_id_of(response)

        def job_state() -> str | None:
            job = d.render_queue.job(job_id)
            return job.state if job is not None else None

        assert until(lambda: job_state() == "done", timeout=10.0)
        done_events = [e for e in seen if e.data.get("job") == job_id and e.data["state"] == "done"]
        assert done_events
        assert done_events[-1].data["out"] == str(tmp_path / "out.mp3")
    finally:
        d.stop()


# --- settings ---------------------------------------------------------------


def test_render_settings_are_declared_with_the_right_defaults() -> None:
    d = make_daemon()
    values = d.settings.values("render")
    assert values["render.mp3_bitrate"] == "64k"
    assert values["render.chapter_gap_ms"] == 1500
    schema = {s["key"]: s for s in d.settings.schema("render")}
    assert schema["render.mp3_bitrate"]["type"] == "choice"
    assert schema["render.mp3_bitrate"]["options"] == ["32k", "48k", "64k", "96k"]
    assert schema["render.chapter_gap_ms"]["type"] == "int"
    assert schema["render.chapter_gap_ms"]["min"] == 0
    assert schema["render.chapter_gap_ms"]["max"] == 10000


def test_sentence_gap_ms_setting_is_declared_under_speech() -> None:
    d = make_daemon()
    values = d.settings.values("speech")
    assert values["speech.sentence_gap_ms"] == 250


def test_render_settings_can_be_changed_over_the_verb(tmp_path: Path) -> None:
    d = make_daemon()
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="cli",
            payload={"key": "render.mp3_bitrate", "value": "96k"},
        )
    )
    assert response.ok
    assert response.data["value"] == "96k"


def test_render_mp3_bitrate_rejects_a_value_outside_the_choices() -> None:
    d = make_daemon()
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="cli",
            payload={"key": "render.mp3_bitrate", "value": "128k"},
        )
    )
    assert not response.ok


# --- a render speaks its own language, with that language's voice ---


def test_a_render_speaks_its_lang_with_that_languages_voice(tmp_path: Path) -> None:
    engine = FakeEngine()
    d = make_daemon(engine=engine)
    response = d.handle(
        Request(Verb.RENDER, "cli", render_payload(tmp_path, lang="fr", format="mp3"))
    )
    assert response.ok, response.error
    job = d.render_queue.job(job_id_of(response))
    assert job is not None
    assert job.lang == "fr"
    # speech.voices' default for fr, since the profile's af_heart is English.
    assert job.voice == "ff_siwis"
    assert until(lambda: "fr" in engine.synthesized_langs)
    assert set(engine.synthesized_langs) == {"fr"}
    d.render_queue.stop()


def test_a_render_with_no_lang_uses_the_default_language(tmp_path: Path) -> None:
    d = make_daemon()
    d.settings.set("speech.detect_language", False)
    response = d.handle(Request(Verb.RENDER, "cli", render_payload(tmp_path, lang="")))
    assert response.ok, response.error
    job = d.render_queue.job(job_id_of(response))
    assert job is not None and job.lang == "en" and job.voice == "af_heart"
    d.render_queue.stop()


def test_a_render_in_an_unsupported_language_is_refused_under_decline(tmp_path: Path) -> None:
    d = make_daemon(engine=FakeEngine(supported=("en",)))
    d.settings.set("speech.unsupported_language", "decline")
    response = d.handle(Request(Verb.RENDER, "cli", render_payload(tmp_path, lang="de")))
    assert not response.ok
    assert "no voice for de" in response.error
    assert d.render_queue.status() == []


def test_a_render_is_accepted_for_an_unloaded_engine_rather_than_declined(
    tmp_path: Path,
) -> None:
    """Review Focus #1: `_render_verb` must not infer availability from
    whether the model happens to be loaded -- only from what languages the
    engine reports (its `supported`, given at construction). A render for a
    language the engine can speak is accepted and left to wait for the
    model, never declined just because nothing is resident yet."""
    from speakd.synth.lazy import LazyEngine

    lazy = LazyEngine(
        lambda: FakeEngine(), name="lazy", sample_rate=24000, supported=lambda: ["en"]
    )
    d = make_daemon(engine=lazy)  # type: ignore[arg-type]
    response = d.handle(Request(Verb.RENDER, "cli", render_payload(tmp_path, lang="en")))
    assert response.ok, response.error
    job = d.render_queue.job(job_id_of(response))
    assert job is not None
    assert until(lambda: job.state == "paused")
    d.render_queue.stop()


def test_a_render_whose_fallback_default_is_itself_unsupported_is_refused_clearly(
    tmp_path: Path,
) -> None:
    """Review Focus #1's known issue: `speech.unsupported_language` not being
    `decline` ordinarily means "speak it in the default instead" -- but if
    the default itself is not something this engine can speak, that would
    only fail mid-render. `_render_language` must refuse up front, naming
    the default rather than the language that was actually requested."""
    engine = FakeEngine(supported=("fr",))  # neither "de" nor the default "en"
    d = make_daemon(engine=engine)
    response = d.handle(Request(Verb.RENDER, "cli", render_payload(tmp_path, lang="de")))
    assert not response.ok
    assert "en" in response.error
    assert d.render_queue.status() == []


def test_a_render_in_an_unsupported_language_falls_back_to_the_default(tmp_path: Path) -> None:
    engine = FakeEngine(supported=("en",))
    d = make_daemon(engine=engine)
    response = d.handle(Request(Verb.RENDER, "cli", render_payload(tmp_path, lang="de")))
    assert response.ok, response.error
    job = d.render_queue.job(job_id_of(response))
    assert job is not None and job.lang == "en"
    assert until(lambda: bool(engine.synthesized_langs))
    assert set(engine.synthesized_langs) == {"en"}
    d.render_queue.stop()
