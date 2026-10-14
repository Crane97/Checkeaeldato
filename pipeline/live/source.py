"""Bloques de audio de una emisión en directo (o de un archivo a ritmo real) con ffmpeg."""

import logging
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from pipeline.audio import SAMPLE_RATE, AudioError, probe_duration

logger = logging.getLogger(__name__)

BLOCK_PATTERN = "block_%05d.flac"
BLOCK_GLOB = "block_*.flac"
DEFAULT_BLOCK_SECONDS = 60
MIN_BLOCK_SECONDS, MAX_BLOCK_SECONDS = 20, 600


class AudioBlock(BaseModel):
    """One stretch of the stream's audio, ready to transcribe."""

    index: int = Field(ge=0)
    path: str
    start: float = Field(ge=0)  # seconds since the stream started
    duration: float = Field(ge=0)
    sample_rate: int = SAMPLE_RATE

    @property
    def end(self) -> float:
        return self.start + self.duration


class BlockSource(Protocol):
    """Yields audio blocks as they become available; `stop()` ends the stream early."""

    def blocks(self) -> Iterator[AudioBlock]: ...

    def stop(self) -> None: ...


def block_command(source: str, out_dir: Path, block_seconds: int, *, realtime: bool) -> list[str]:
    """ffmpeg command that writes `source` as consecutive mono 16 kHz FLAC blocks.

    `realtime` (-re) makes a recorded file play at its own pace, as if it were being
    broadcast; a live stream already arrives at that pace."""
    if not MIN_BLOCK_SECONDS <= block_seconds <= MAX_BLOCK_SECONDS:
        raise ValueError(
            f"El bloque debe durar entre {MIN_BLOCK_SECONDS} y {MAX_BLOCK_SECONDS} segundos"
        )
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *(["-re"] if realtime else []),
        "-i",
        source,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "flac",
        "-f",
        "segment",
        "-segment_time",
        str(block_seconds),
        "-reset_timestamps",
        "1",
        str(out_dir / BLOCK_PATTERN),
    ]


def is_local_file(source: str) -> bool:
    return "://" not in source and Path(source).is_file()


class FfmpegBlockSource:
    """Runs ffmpeg in the background and yields each block once ffmpeg has moved on to the
    next one (or has finished), so a block is never read half-written."""

    def __init__(
        self,
        source: str,
        out_dir: Path,
        *,
        block_seconds: int = DEFAULT_BLOCK_SECONDS,
        realtime: bool | None = None,
        poll_seconds: float = 1.0,
        popen: Callable[..., Any] = subprocess.Popen,
        probe: Callable[[Path], float] = probe_duration,
        sleep: Callable[[float], None] = time.sleep,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.source = source
        self.out_dir = out_dir
        self.block_seconds = block_seconds
        self.realtime = is_local_file(source) if realtime is None else realtime
        self._poll_seconds = poll_seconds
        self._popen = popen
        self._probe = probe
        self._sleep = sleep
        self._which = which
        self._process: Any = None
        self._stopped = False

    def blocks(self) -> Iterator[AudioBlock]:
        if self._which("ffmpeg") is None:
            raise AudioError(
                "No se encuentra ffmpeg; instálalo "
                "(brew install ffmpeg / winget install Gyan.FFmpeg)"
            )
        self.out_dir.mkdir(parents=True, exist_ok=True)
        cmd = block_command(self.source, self.out_dir, self.block_seconds, realtime=self.realtime)
        logger.info("Leyendo %s en bloques de %d s", self.source, self.block_seconds)
        self._process = self._popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        next_index = 0
        elapsed = 0.0
        while True:
            finished = self._process.poll() is not None
            files = sorted(self.out_dir.glob(BLOCK_GLOB))
            # The last file is still being written unless ffmpeg has finished.
            ready = files if finished else files[:-1]
            for path in ready[next_index:]:
                duration = self._probe(path)
                block = AudioBlock(
                    index=next_index, path=str(path), start=elapsed, duration=duration
                )
                elapsed += duration
                next_index += 1
                yield block
            if finished:
                break
            self._sleep(self._poll_seconds)

        code = self._process.returncode
        if code != 0 and not self._stopped:
            stderr = self._process.stderr.read() if self._process.stderr else b""
            tail = "\n".join(stderr.decode("utf-8", "replace").strip().splitlines()[-5:])
            raise AudioError(f"ffmpeg terminó con código {code}:\n{tail}")

    def stop(self) -> None:
        self._stopped = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
