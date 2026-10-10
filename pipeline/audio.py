"""Extracción de audio con ffmpeg."""

import json
import shutil
import subprocess
from pathlib import Path

from pipeline.models import AudioInfo

# Lossless mono 16 kHz: enough for speech recognition and ~2 MB per minute.
AUDIO_FILE = "audio.flac"
SAMPLE_RATE = 16_000


class AudioError(RuntimeError):
    """ffmpeg is missing or failed; the message is shown to the user."""


def _run(cmd: list[str]) -> str:
    if shutil.which(cmd[0]) is None:
        raise AudioError(f"No se encuentra {cmd[0]}; instálalo con `brew install ffmpeg`")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise AudioError(f"{cmd[0]} falló (código {result.returncode}):\n{tail}")
    return result.stdout


def probe_duration(path: Path) -> float:
    out = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    )
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AudioError(f"No se pudo leer la duración de {path}") from exc


def extract_audio(video: Path, out_dir: Path) -> AudioInfo:
    """Extract the audio track of `video` as mono 16 kHz FLAC into `out_dir`."""
    out = out_dir / AUDIO_FILE
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "flac",
            str(out),
        ]
    )
    return AudioInfo(path=str(out), duration=probe_duration(out), sample_rate=SAMPLE_RATE)


def purge_audio(info: AudioInfo) -> None:
    Path(info.path).unlink(missing_ok=True)
