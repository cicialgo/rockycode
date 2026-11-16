"""Talk mode pure core — the conversation that turns itself. No mic, no
ffmpeg, no key, no network: every event and rule is deterministic.
"""
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.environ.pop("ROCKYCODE_API_KEY", None)  # hermetic: no ambient key

from rockycode.engine.talk import (  # noqa: E402
    BARGE,
    LISTEN,
    NONE,
    SUBMIT,
    TRANSCRIBE,
    TalkMachine,
    TalkPhase,
)


def test_happy_conversation():
    seen = []
    m = TalkMachine(notify=lambda old, new: seen.append((old, new)))
    assert m.phase == TalkPhase.PAUSED
    assert m.start() == (LISTEN, "")
    assert m.phase == TalkPhase.LISTENING
    # utterance → transcribe → submit → think → reply → (no readback) → listen
    assert m.heard_end() == (TRANSCRIBE, "")
    assert m.phase == TalkPhase.TRANSCRIBING
    assert m.transcript("把测试跑一遍") == (SUBMIT, "把测试跑一遍")
    assert m.phase == TalkPhase.THINKING
    m.expect_readback(False)
    assert m.reply_done() == (LISTEN, "")
    assert m.phase == TalkPhase.LISTENING
    assert seen == [(TalkPhase.PAUSED, TalkPhase.LISTENING),
                    (TalkPhase.LISTENING, TalkPhase.TRANSCRIBING),
                    (TalkPhase.TRANSCRIBING, TalkPhase.THINKING),
                    (TalkPhase.THINKING, TalkPhase.LISTENING)], seen
    print("  happy path: listen → transcribe → submit → think → listen  ✓")


def test_readback_tail_delays_listening():
    m = TalkMachine()
    m.start()
    m.heard_end()
    m.transcript("hi")
    m.expect_readback(True)   # the speak queue is non-empty
    assert m.reply_done() == (NONE, "")      # wait for the tail…
    assert m.phase == TalkPhase.SPEAKING
    assert m.readback_done() == (LISTEN, "")  # …then listen
    assert m.phase == TalkPhase.LISTENING
    print("  readback: listening waits for the spoken tail  ✓")


def test_barge_in():
    m = TalkMachine()
    m.start()
    m.heard_end()
    m.transcript("干活")
    assert m.phase == TalkPhase.THINKING
    # speech while thinking → barge: cancel the turn, listen
    assert m.speech() == (BARGE, "")
    assert m.phase == TalkPhase.LISTENING
    assert m.barges == 1
    # speech while speaking → same
    m.heard_end()
    m.transcript("again")
    m.expect_readback(True)
    m.reply_done()
    assert m.phase == TalkPhase.SPEAKING
    assert m.speech() == (BARGE, "")
    assert m.phase == TalkPhase.LISTENING and m.barges == 2
    # speech while already listening → the VAD owns it, nothing to barge
    assert m.speech() == (NONE, "")
    # events that arrive late (superseded by the barge) are no-ops
    assert m.transcript("late") == (NONE, "")
    print("  barge: speech during think/speak cancels and listens; late events no-op  ✓")


def test_empty_transcript_keeps_listening():
    m = TalkMachine()
    m.start()
    m.heard_end()
    action, msg = m.transcript("")
    assert action == LISTEN and "nothing" in msg
    assert m.phase == TalkPhase.LISTENING
    # a whitespace transcript is also empty
    m.heard_end()
    assert m.transcript("   ")[0] == LISTEN
    print("  empty transcript: never submitted — one muted line, keep listening  ✓")


def test_pause_and_stop():
    m = TalkMachine()
    m.start()
    m.heard_end()
    m.transcript("x")
    assert m.phase == TalkPhase.THINKING
    # pausing mid-turn stops the NEXT listen, not the running turn
    assert m.stop() == (NONE, "talk paused")
    assert m.phase == TalkPhase.PAUSED
    assert m.reply_done() == (NONE, "")  # no auto-listen while paused
    assert m.turn_cancelled() == (NONE, "")
    # start again → listening resumes; a second start is a no-op
    assert m.start() == (LISTEN, "")
    assert m.start() == (NONE, "")
    print("  pause: mid-turn stop kills the auto-listen, never the turn  ✓")


def test_turn_cancelled_elsewhere():
    m = TalkMachine()
    m.start()
    m.heard_end()
    m.transcript("x")
    # Esc or a typed message cancelled the turn (not a barge) → keep talking
    assert m.turn_cancelled() == (LISTEN, "")
    assert m.phase == TalkPhase.LISTENING
    print("  esc/typed cancel: talk resumes listening  ✓")


def test_typed_turn_yields_the_mic():
    m = TalkMachine()
    m.start()
    m.heard_end()  # an utterance is in flight…
    # …the user types instead: the mic yields, the typed turn owns the phase
    assert m.typed_turn() == (NONE, "")
    assert m.phase == TalkPhase.THINKING
    # a typed turn while thinking (talk-submitted) is a no-op
    assert m.typed_turn() == (NONE, "")
    # a typed turn while paused changes nothing
    m.stop()
    assert m.typed_turn() == (NONE, "")
    assert m.phase == TalkPhase.PAUSED
    print("  typed turn: the mic yields; talk turns and paused stays put  ✓")


test_happy_conversation()
test_readback_tail_delays_listening()
test_barge_in()
test_empty_transcript_keeps_listening()
test_pause_and_stop()
test_turn_cancelled_elsewhere()
test_typed_turn_yields_the_mic()
print("TALK CORE SMOKE OK — turn-taking · readback tail · barge · empty · pause · cancel · typed. amaze!")
