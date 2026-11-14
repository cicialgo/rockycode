"""Mic capture via ffmpeg — raw PCM over a pipe into a bounded rolling
buffer, no new dependencies (ffmpeg is already rocky's image-downscale
fallback, and every recording backend needs *some* binary).

Backends are ffmpeg's own input devices, selected by platform:

    darwin   -f avfoundation -i :<device>
    linux    -f pulse -i <device>
    windows  -f dshow -i audio=<device>

`<device>` is `default` unless ROCKYCODE_VOICE_DEVICE pins an index ("0")
or a name. ROCKYCODE_FFMPEG pins the binary path (a machine can carry a
broken ffmpeg early on PATH — the /usr/local vs /opt/homebrew split).

WHY A PERSISTENT PROCESS: avfoundation's first device open takes ~10 s on
a real Mac (measured 9.4–12.2 s across three devices) — a per-capture
spawn would lose the first ten seconds of speech every time. The recorder
therefore stays alive for the session once started: the first hotkey pays
the warm-up (the line says so), every later capture is instant, and
segments are cut from the stream via mark()/finish_segment() instead of
process restarts. The mic stays hot until /exit — macOS shows the orange
mic dot, same as any call app. Output is 16-bit mono PCM at
engine/voice.SAMPLE_RATE (16 kHz — inside the ASR-accepted 8k–48k span;
a 115 s segment is ~3.7 MB, well under the 8 MiB upload limit). The app
wraps the segment in a WAV header (voice.pcm_to_wav) before upload.

Failure signatures (found live): a TCC-blocked mic makes the process sit
alive and silent — warm-up times out and the error names the permission
fix. A bad device name kills the process fast — stderr says why, and
`default` falls back to device `0` once (an explicit pin never does).
"""
from __future__ import annotations

import asyncio
import collections
import os
import platform
import shutil
from typing import Optional

from rockycode.engine import voice

FFMPEG_ENV = "ROCKYCODE_FFMPEG"
DEVICE_ENV = "ROCKYCODE_VOICE_DEVICE"
WARM_TIMEOUT_S = 20.0  # measured device-open latency is ~10 s; TCC stall = forever

_INPUTS = {
    "Darwin": ("-f", "avfoundation"),
    "Linux": ("-f", "pulse"),
    "Windows": ("-f", "dshow"),
}


class RecorderError(Exception):
    """Recording could not start. Message is user-facing (a fix)."""


def ffmpeg_bin() -> str:
    """The binary to spawn — env-pinned or PATH's."""
    return (os.getenv(FFMPEG_ENV) or "ffmpeg").strip() or "ffmpeg"


def ffmpeg_input_args(system: Optional[str] = None, device: Optional[str] = None) -> tuple[str, ...]:
    """The input-device args for this platform (pure — smoke-tested).
    `device` wins over the env override, which wins over `default`."""
    args = _INPUTS.get(system or platform.system())
    if args is None:
        return ()
    dev = (device or os.getenv(DEVICE_ENV) or "default").strip() or "default"
    if (system or platform.system()) == "Windows":
        return (*args, "-i", f"audio={dev}")
    if (system or platform.system()) == "Darwin":
        return (*args, "-i", f":{dev}")
    return (*args, "-i", dev)


def ffmpeg_available() -> Optional[str]:
    """None = fine; else the user-facing hint for a missing ffmpeg."""
    if shutil.which(ffmpeg_bin()) is None:
        return (f"ffmpeg not found — install it (brew install ffmpeg) or point "
                f"{FFMPEG_ENV} at an ffmpeg binary")
    if not ffmpeg_input_args():
        return f"no known mic input for {platform.system()} — voice recording is not wired here"
    return None


def _permission_error(devices: tuple[str, ...]) -> RecorderError:
    return RecorderError(
        f"the microphone stays silent ({', '.join(f'`{d}`' for d in devices)} tried) — on "
        f"macOS this is almost always the microphone permission: System Settings → "
        f"Privacy & Security → Microphone → your terminal app (then restart rocky). "
        f"Pin a device with {DEVICE_ENV} if several mics exist"
    )


class Recorder:
    """One live mic session, kept warm across captures. start() spawns ffmpeg
    writing PCM to stdout (pipe → rolling buffer); mark() begins a segment,
    finish_segment() cuts it; stop() ends the session. stop_requested is the
    hotkey's manual endpoint — the app's capture loop checks it."""

    def __init__(self, *, rate: int = voice.SAMPLE_RATE) -> None:
        self._rate = rate
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._drain: Optional[asyncio.Task] = None
        self._stderr: collections.deque = collections.deque(maxlen=5)
        self._buf = bytearray()
        self._cap = rate * 2 * (voice.MAX_RECORD_S + 15)  # ~130 s of s16le
        self._vad_cur = 0   # bytes already handed to the VAD
        self._mark = 0      # segment start index into _buf
        self._first = asyncio.Event()   # the device delivered bytes
        self._dead = asyncio.Event()    # ffmpeg exited on its own
        self.stop_requested = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def started(self) -> bool:
        return self._proc is not None

    @property
    def warm(self) -> bool:
        return self._first.is_set()

    async def start(self) -> None:
        """Spawn ffmpeg and wait for the first audio bytes (up to
        WARM_TIMEOUT_S). Raises RecorderError with the fix when ffmpeg is
        missing, the device cannot open, or the mic never delivers."""
        hint = ffmpeg_available()
        if hint:
            raise RecorderError(hint)
        if self.started:
            return  # warm from an earlier capture — segments are instant
        # A pinned device is tried alone (an explicit choice must not be
        # silently replaced); otherwise `default` first, then index 0 — some
        # ffmpeg builds never resolve `:default`.
        pinned = (os.getenv(DEVICE_ENV) or "").strip()
        candidates = (pinned,) if pinned else ("default", "0")
        tried = []
        for device in candidates:
            tried.append(device)
            self._first.clear()
            self._dead.clear()
            args = ("-loglevel", "error", *ffmpeg_input_args(device=device),
                    "-ac", "1", "-ar", str(self._rate), "-f", "s16le", "pipe:1")
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin(), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self._proc = proc
            self._reader = asyncio.create_task(self._read_loop())
            self._drain = asyncio.create_task(self._drain_stderr())
            first_t = asyncio.ensure_future(self._first.wait())
            dead_t = asyncio.ensure_future(self._dead.wait())
            done, _ = await asyncio.wait(
                {first_t, dead_t},
                return_when=asyncio.FIRST_COMPLETED, timeout=WARM_TIMEOUT_S,
            )
            for t in (first_t, dead_t):  # never leak a pending wait
                if not t.done():
                    t.cancel()
            if self._first.is_set() and self.running:
                return  # warm — capture immediately
            if self._dead.is_set():
                err = "".join(self._stderr).strip().splitlines()[-1] if self._stderr else ""
                await self._teardown_proc()
                raise RecorderError(
                    f"the microphone did not start (ffmpeg: {err or 'no device'}) — pin a "
                    f"device with {DEVICE_ENV} (index like `0`, or a name; list them: ffmpeg "
                    f"-f avfoundation -list_devices true -i dummy)"
                )
            if not done:  # warm-up timed out: alive but silent = the TCC stall
                await self._teardown_proc()
                raise _permission_error(tuple(tried))
            # delivered bytes but the process died before start() returned —
            # rare race; treat as a failed device and let the next candidate try
            await self._teardown_proc()
        raise RecorderError(
            f"the microphone did not start — ffmpeg died on every device "
            f"({', '.join(tried)}) — pin one with {DEVICE_ENV}"
        )

    async def stop(self) -> None:
        """End the session — the app calls this on /exit (never per capture;
        the warm process is the whole point)."""
        await self._teardown_proc()
        self._buf = bytearray()
        self._vad_cur = 0
        self._mark = 0
        self.stop_requested = False

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:  # EOF: ffmpeg died
                self._dead.set()
                return
            if not self._first.is_set():
                self._first.set()
            self._buf.extend(chunk)
            over = len(self._buf) - self._cap
            if over > 0:
                del self._buf[:over]
                self._vad_cur = max(0, self._vad_cur - over)
                self._mark = max(0, self._mark - over)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode(errors="replace").strip())

    def mark(self) -> None:
        """Begin a capture segment: everything from now until finish_segment
        is that clip (whatever the warm mic recorded before is dropped)."""
        self._mark = len(self._buf)
        self._vad_cur = max(self._vad_cur, self._mark)

    def read_new(self) -> bytes:
        """The bytes since the last read_new (synchronous; the worker calls
        it via to_thread so buffer copies stay off the message pump)."""
        if self._vad_cur >= len(self._buf):
            return b""
        data = bytes(self._buf[self._vad_cur:])
        self._vad_cur = len(self._buf)
        return data

    def finish_segment(self) -> bytes:
        """The segment's PCM since mark() — the app wraps it in WAV."""
        seg = bytes(self._buf[self._mark:])
        self._mark = len(self._buf)
        self._vad_cur = max(self._vad_cur, self._mark)
        return seg

    async def _teardown_proc(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._drain is not None:
            self._drain.cancel()
            self._drain = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
