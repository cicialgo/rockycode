"""Mic capture via ffmpeg — raw PCM to a temp file, the VAD reads it as it
grows (no new dependencies: ffmpeg is already rocky's image-downscale
fallback, and every recording backend needs *some* binary).

Backends are ffmpeg's own input devices, selected by platform:

    darwin   -f avfoundation -i :default
    linux    -f pulse -i default
    windows  -f dshow -i audio=default

Output is 16-bit mono PCM at engine/voice.SAMPLE_RATE (16 kHz — inside the
ASR-accepted 8k–48k span, and small enough that a 115 s cap is ~3.7 MB, well
under the 8 MiB upload limit). The app wraps the PCM in a WAV header
(voice.pcm_to_wav) before upload.

ffmpeg's own stdout/stderr are kept quiet; stderr is drained into a bounded
buffer so a dying device can still say why.
"""
from __future__ import annotations

import asyncio
import collections
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from rockycode.engine import voice

_INPUTS = {
    "Darwin": ("-f", "avfoundation", "-i", ":default"),
    "Linux": ("-f", "pulse", "-i", "default"),
    "Windows": ("-f", "dshow", "-i", "audio=default"),
}


class RecorderError(Exception):
    """Recording could not start. Message is user-facing (a fix)."""


def ffmpeg_input_args(system: Optional[str] = None) -> tuple[str, ...]:
    """The input-device args for this platform (pure — smoke-tested)."""
    return _INPUTS.get(system or platform.system(), ())


def ffmpeg_available() -> Optional[str]:
    """None = fine; else the user-facing hint for a missing ffmpeg."""
    if shutil.which("ffmpeg") is None:
        return "ffmpeg not found — install it (brew install ffmpeg) or use a system mic recorder"
    if not ffmpeg_input_args():
        return f"no known mic input for {platform.system()} — voice recording is not wired here"
    return None


class Recorder:
    """One recording session: start() spawns ffmpeg writing raw PCM; the app
    polls read_new() (feeding the VAD) and stop() ends it. stop_requested is
    the hotkey's manual endpoint — the worker checks it, stop() honors it."""

    def __init__(self, *, rate: int = voice.SAMPLE_RATE) -> None:
        self._rate = rate
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pcm: Optional[Path] = None
        self._fd = None
        self._offset = 0
        self._stderr: collections.deque = collections.deque(maxlen=5)
        self._drain: Optional[asyncio.Task] = None
        self.stop_requested = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> Path:
        """Spawn ffmpeg; returns the raw-PCM path. Raises RecorderError with
        the fix when ffmpeg or the mic device is missing."""
        hint = ffmpeg_available()
        if hint:
            raise RecorderError(hint)
        if self._proc is not None:
            raise RecorderError("a recording is already running")
        tmp = Path(tempfile.mkdtemp(prefix="rockyvoice-"))
        pcm = tmp / "voice.pcm"
        args = ("-loglevel", "error", *ffmpeg_input_args(),
                "-ac", "1", "-ar", str(self._rate), "-f", "s16le", "-y", str(pcm))
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        self._pcm = pcm
        self._drain = asyncio.create_task(self._drain_stderr())
        # The device open is slow (macOS: TCC + avfoundation enumeration can
        # take seconds) — the process is alive but the output file does not
        # exist yet. Wait for the file; a process that dies first is a device
        # failure and its stderr says why.
        for _ in range(60):  # up to 3 s
            if pcm.exists():
                break
            if not self.running:
                err = "".join(self._stderr).strip().splitlines()[-1] if self._stderr else ""
                await self.stop()
                raise RecorderError(
                    f"the microphone did not start (ffmpeg: {err or 'no device'}) — "
                    f"check System Settings → Privacy & Security → Microphone for your terminal"
                )
            await asyncio.sleep(0.05)
        if not pcm.exists():
            await self.stop()
            raise RecorderError(
                "ffmpeg started but produced no audio file — the mic device is "
                "not delivering; check System Settings → Privacy & Security → Microphone"
            )
        self._fd = open(pcm, "rb")  # noqa: SIM115 — closed in stop()
        return pcm

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode(errors="replace").strip())

    def read_new(self) -> bytes:
        """The bytes appended since the last call (synchronous; the worker
        calls it via to_thread so file I/O stays off the message pump)."""
        if self._fd is None:
            return b""
        self._fd.seek(self._offset)
        data = self._fd.read()
        self._offset += len(data)
        return data

    async def stop(self) -> Path:
        """End the session; returns the PCM path (the app wraps it in WAV)."""
        if self._drain is not None:
            self._drain.cancel()
            self._drain = None
        if self._fd is not None:
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
        assert self._pcm is not None
        return self._pcm
