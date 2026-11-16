"""Talk mode — hands-free voice conversation with rocky (plan A of the
voice roadmap: A talk mode → C local streaming ASR → B realtime vendor
only if the cost proves controllable).

The PURE core: which phase the conversation is in, what each event means,
and the turn-taking rules. No I/O, no asyncio, no Textual — the app drives
it exactly like cron.py's bookkeeping and voice.py's machine. The tap
mode's parts are reused untouched (recorder, VAD, ASR, sentence TTS).

Talk mode differs from tap mode in one way: the conversation TURNS
ITSELF.

    utterance ends        → transcript auto-submits (no Enter)
    reply finishes        → the spoken tail plays out, then the warm mic
                            starts listening again (no hotkey)
    speech while rocky    → BARGE-IN: the audio cuts, the turn is
    thinks or speaks        cancelled, listening starts — like a person

An empty transcript (silence/noise) is not submitted — the mic just keeps
listening, with one muted line. The transcript still lands in history
exactly as typing would, so permissions, loops and trajectories behave
identically to a typed turn.

Assumptions carried from phase 1: headphones/headset — the mic does not
hear rocky's own TTS (open-speaker AEC is a later plan; the recorder's
stall-restart is the safety net if it does).
"""
from __future__ import annotations

from typing import Callable, Optional

# A talk turn reads at most this many chars of the reply aloud — coding
# replies run long, and reading 40 lines of code is noise. The transcript
# keeps everything; one muted line says the rest is on screen.
TALK_SPOKEN_CAP = 1500


class TalkPhase:
    PAUSED = "paused"            # talk off — tap mode (hotkey capture) only
    LISTENING = "listening"      # mic hot, VAD looking for an utterance end
    TRANSCRIBING = "transcribing"  # clip uploading to ASR
    THINKING = "thinking"        # rocky's turn is running (tools included)
    SPEAKING = "speaking"        # readback audible — barge-in is armed

    @classmethod
    def all(cls) -> tuple[str, ...]:
        return (cls.PAUSED, cls.LISTENING, cls.TRANSCRIBING, cls.THINKING, cls.SPEAKING)


# What the app must DO for an event, decided here (pure policy).
NONE, LISTEN, TRANSCRIBE, SUBMIT, BARGE = "none", "listen", "transcribe", "submit", "barge"


class TalkMachine:
    """The conversation's phase + the turn-taking rules.

    Events arrive from the app's plumbing (VAD endings, transcripts, turn
    completions, readback drains, detected speech) and the machine answers
    (action, message) — the app executes the action. `notify` mirrors the
    LoopBook pattern so tests and the chip can observe without I/O.
    """

    def __init__(self, *, notify: Optional[Callable[[str, str], None]] = None) -> None:
        self._notify = notify
        self.phase = TalkPhase.PAUSED
        self.barges = 0  # session counter — the chip can show "interrupted you n×"

    # ---- control -------------------------------------------------------------

    def start(self) -> tuple[str, str]:
        """Turn talk on: listening begins immediately (the warm mic is marked
        and the VAD armed by the app)."""
        if self.phase == TalkPhase.PAUSED:
            self._set(TalkPhase.LISTENING)
            return LISTEN, ""
        return NONE, ""

    def stop(self) -> tuple[str, str]:
        """Turn talk off (or pause during a turn — the turn itself finishes
        normally; the next listen is simply not armed)."""
        if self.phase != TalkPhase.PAUSED:
            self._set(TalkPhase.PAUSED)
            return NONE, "talk paused"
        return NONE, ""

    # ---- the conversation turning itself --------------------------------------

    def heard_end(self) -> tuple[str, str]:
        """The VAD ended an utterance while listening."""
        if self.phase == TalkPhase.LISTENING:
            self._set(TalkPhase.TRANSCRIBING)
            return TRANSCRIBE, ""
        return NONE, ""  # a barge race — the new listening already owns the mic

    def transcript(self, text: str) -> tuple[str, str]:
        """ASR answered. Text → submit as the next turn; empty (silence or
        noise) → keep listening, one muted line."""
        if self.phase != TalkPhase.TRANSCRIBING:
            return NONE, ""  # superseded (a barge cut this clip short)
        text = (text or "").strip()
        if text:
            self._set(TalkPhase.THINKING)
            return SUBMIT, text
        self._set(TalkPhase.LISTENING)
        return LISTEN, "heard nothing — still listening"

    def reply_done(self) -> tuple[str, str]:
        """Rocky's turn finished. The spoken tail (if any) plays out first;
        when the readback queue drains the app calls readback_done()."""
        if self.phase == TalkPhase.THINKING:
            if self._readback_expected:
                self._set(TalkPhase.SPEAKING)
                return NONE, ""
            self._set(TalkPhase.LISTENING)
            return LISTEN, ""
        return NONE, ""

    def readback_done(self) -> tuple[str, str]:
        """The last sentence finished playing — listen again."""
        if self.phase == TalkPhase.SPEAKING:
            self._set(TalkPhase.LISTENING)
            return LISTEN, ""
        return NONE, ""

    def speech(self) -> tuple[str, str]:
        """Detected speech while rocky was THINKING or SPEAKING — barge in:
        cut the audio, cancel the turn, start listening. Like a person."""
        if self.phase in (TalkPhase.THINKING, TalkPhase.SPEAKING):
            self.barges += 1
            self._set(TalkPhase.LISTENING)
            return BARGE, ""
        return NONE, ""  # already listening — the VAD owns the utterance

    # ---- plumbing -------------------------------------------------------------

    def turn_cancelled(self) -> tuple[str, str]:
        """The turn was cancelled by something OTHER than a barge (Esc, a new
        typed message). Talk mode resumes listening."""
        if self.phase == TalkPhase.THINKING:
            self._set(TalkPhase.LISTENING)
            return LISTEN, ""
        return NONE, ""

    def typed_turn(self) -> tuple[str, str]:
        """A typed message starts a turn while talk is on — the mic worker
        yields (its in-flight utterance is discarded, never submitted) and
        barge protection arms for this turn like any talk turn."""
        if self.phase in (TalkPhase.LISTENING, TalkPhase.TRANSCRIBING):
            self._set(TalkPhase.THINKING)
        return NONE, ""

    # The app tells the machine whether readback is expected BEFORE calling
    # reply_done() (it knows if the speak queue is non-empty). This flag
    # keeps the machine pure — it never touches the queue itself.
    _readback_expected = False

    def expect_readback(self, expected: bool) -> None:
        self._readback_expected = expected

    def _set(self, phase: str) -> None:
        old, self.phase = self.phase, phase
        if self._notify is not None and old != phase:
            self._notify(old, phase)
