"""Mic capture via ffmpeg — raw PCM to a temp file, the VAD reads it as it
grows (no new dependencies: ffmpeg is already rocky's image-downscale
fallback, and every recording backend needs *some* binary).

Backends are ffmpeg's own input devices, selected by platform:

    darwin   -f avfoundation -i :<device>
    linux    -f pulse -i <device>
    windows  -f dshow -i audio=<device>

`<device>` is `default` unless ROCKYCODE_VOICE_DEVICE pins an index ("0")
or a name — some ffmpeg builds never resolve `:default`, so when no audio
file appears the recorder retries device `0` once before reporting.
ROCKYCODE_FFMPEG pins the binary path (a machine can carry a broken
ffmpeg early on PATH — see the /usr/local vs /opt/homebrew split).

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
import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from rockycode.engine import voice

FFMPEG_ENV = "ROCKYCODE_FFMPEG"
DEVICE_ENV = "ROCKYCODE_VOICE_DEVICE"

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
        the fix when ffmpeg is missing, the mic device cannot open (TCC
        permission is the usual cause — the process hangs, alive but
        fileless), or every device candidate fails."""
        hint = ffmpeg_available()
        if hint:
            raise RecorderError(hint)
        if self._proc is not None:
            raise RecorderError("a recording is already running")
        tmp = Path(tempfile.mkdtemp(prefix="rockyvoice-"))
        pcm = tmp / "voice.pcm"
        self._pcm = pcm
        # A pinned device is tried alone (an explicit choice must not be
        # silently replaced); otherwise `default` first, then index 0 — some
        # ffmpeg builds never resolve `:default` (found on ffmpeg 9/arm64
        # with two mics: the open just stalls).
        pinned = (os.getenv(DEVICE_ENV) or "").strip()
        candidates = [pinned] if pinned else ["default", "0"]
        last_err = ""
        for device in candidates:
            args = ("-loglevel", "error", *ffmpeg_input_args(device=device),
                    "-ac", "1", "-ar", str(self._rate), "-f", "s16le", "-y", str(pcm))
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin(), *args,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            self._proc = proc
            self._drain = asyncio.create_task(self._drain_stderr())
            # The device open is slow (macOS: TCC + avfoundation enumeration
            # can take seconds) — the process is alive but the output file
            # does not exist yet. Wait for the file; a process that dies
            # first is a device failure and its stderr says why.
            for _ in range(60):  # up to 3 s
                if pcm.exists():
                    break
                if not self.running:
                    break
                await asyncio.sleep(0.05)
            if pcm.exists():
                # The file existing is not enough: a TCC-blocked open can
                # create it and then hang forever. A delivering mic writes
                # bytes immediately (raw PCM); still 0 after 0.5 s = failed.
                for _ in range(10):
                    if pcm.stat().st_size > 0:
                        self._fd = open(pcm, "rb")  # noqa: SIM115 — closed in stop()
                        return pcm
                    if not self.running:
                        break
                    await asyncio.sleep(0.05)
            last_err = "".join(self._stderr).strip().splitlines()[-1] if self._stderr else ""
            await self.stop()
            pcm.unlink(missing_ok=True)
        if last_err:
            raise RecorderError(
                f"the microphone did not start (ffmpeg: {last_err}) — check System "
                f"Settings → Privacy & Security → Microphone for your terminal, or pin "
                f"a device: {DEVICE_ENV}=0 (list them: ffmpeg -f avfoundation "
                f"-list_devices true -i dummy)"
            )
        raise RecorderError(
            f"the microphone did not start — the device open stalls, which on macOS "
            f"is usually the microphone permission (System Settings → Privacy & "
            f"Security → Microphone → your terminal app); pin a device with "
            f"{DEVICE_ENV} (index or name) if several mics exist"
        )

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
