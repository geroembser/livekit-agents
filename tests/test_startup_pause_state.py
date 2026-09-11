"""Exercise false-interruption resumes racing the first playback callback.

Provider fakes keep the real session, audio forwarding, and pause lifecycle in use.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from livekit.agents import Agent, AgentFalseInterruptionEvent, AgentStateChangedEvent
from livekit.agents.voice.io import PlaybackFinishedEvent, PlaybackStartedEvent

from .fake_session import FakeActions, create_session, run_session

pytestmark = [pytest.mark.unit, pytest.mark.virtual_time, pytest.mark.no_concurrent]


@pytest.mark.parametrize("later_transcript", ["", "Stop"])
@pytest.mark.parametrize("onset_guard", [None, 1.0])
async def test_startup_resume_preserves_started_playback(
    later_transcript: str, onset_guard: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    actions = FakeActions()
    actions.add_user_speech(0.1, 0.4, "Tell me a story.")
    actions.add_llm("Here is a short story.", ttft=0.02, duration=0.02)
    actions.add_tts(3.0, ttfb=0.02, duration=0.02)
    actions.add_user_speech(2.0, 2.6, later_transcript, stt_delay=0.1)
    if later_transcript:
        actions.add_llm("Okay.", ttft=0.02, duration=0.02)
        actions.add_tts(0.3, ttfb=0.02, duration=0.02)

    session = create_session(
        actions,
        can_pause_audio=True,
        turn_handling={"interruption": {"min_words": 1}},
        extra_kwargs={"aec_onset_guard_duration": onset_guard},
    )
    output = session.output.audio
    assert output is not None
    states: list[AgentStateChangedEvent] = []
    finished: list[PlaybackFinishedEvent] = []
    resume_states: list[tuple[str, bool, int, float]] = []
    initial_guard: list[float] = []
    saved_states: list[str] = []
    recognition_starts: MagicMock | None = None

    def on_state(ev: AgentStateChangedEvent) -> None:
        states.append(ev)
        if ev.new_state == "speaking" and not initial_guard:
            session._loop.call_soon(lambda: initial_guard.append(session._aec_onset_guard_until))

    def on_playback(ev: PlaybackStartedEvent) -> None:
        nonlocal recognition_starts
        if saved_states:
            return
        activity = session._activity
        assert activity is not None and activity._audio_recognition is not None
        recognition = activity._audio_recognition
        recognition_starts = MagicMock(wraps=recognition._on_start_of_agent_speech)
        monkeypatch.setattr(recognition, "_on_start_of_agent_speech", recognition_starts)
        # Playback resolves first_frame_fut, but its state callback runs on the
        # next loop iteration. A speech-start event can still save "thinking" here.
        activity.on_start_of_speech(None, time.time())
        assert activity._paused_speech is not None
        saved_states.append(activity._paused_speech.agent_state)
        session._loop.call_later(0.05, activity.on_end_of_speech, None)

    def on_resume(ev: AgentFalseInterruptionEvent) -> None:
        activity = session._activity
        assert activity is not None and activity._audio_recognition is not None
        assert recognition_starts is not None
        resume_states.append(
            (
                session.agent_state,
                activity._audio_recognition._agent_speaking,
                recognition_starts.call_count,
                session._aec_onset_guard_until,
            )
        )

    session.on("agent_state_changed", on_state)
    session.on("agent_false_interruption", on_resume)
    output.on("playback_started", on_playback)
    output.on("playback_finished", finished.append)

    await asyncio.wait_for(run_session(session, Agent(instructions="test")), timeout=15)

    assert saved_states == ["thinking"]
    assert len(resume_states) == 1
    assert resume_states[0] == ("speaking", True, 1, initial_guard[0])
    assert ("speaking", "thinking") not in [(ev.old_state, ev.new_state) for ev in states]
    assert finished[0].interrupted is bool(later_transcript)
    if not later_transcript:
        assert finished[0].playback_position == pytest.approx(3.0, abs=0.02)
