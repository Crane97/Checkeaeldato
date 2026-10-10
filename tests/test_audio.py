import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import audio
from pipeline.audio import AudioError, extract_audio, probe_duration, purge_audio


class FakeRun:
    """Stands in for subprocess.run, answering ffprobe with a fixed duration."""

    def __init__(self, returncode: int = 0, duration: str = "12.5", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.duration = duration
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        stdout = json.dumps({"format": {"duration": self.duration}}) if cmd[0] == "ffprobe" else ""
        return subprocess.CompletedProcess(cmd, self.returncode, stdout, self.stderr)


@pytest.fixture
def fake_run(monkeypatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(audio.subprocess, "run", fake)
    monkeypatch.setattr(audio.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    return fake


def test_extract_audio_builds_mono_16k_flac(fake_run: FakeRun, tmp_path: Path):
    info = extract_audio(tmp_path / "debate.mp4", tmp_path)

    ffmpeg_cmd = fake_run.calls[0]
    assert ffmpeg_cmd[0] == "ffmpeg"
    assert ffmpeg_cmd[ffmpeg_cmd.index("-ac") + 1] == "1"
    assert ffmpeg_cmd[ffmpeg_cmd.index("-ar") + 1] == "16000"
    assert ffmpeg_cmd[-1] == str(tmp_path / "audio.flac")
    assert info.duration == 12.5
    assert info.path == str(tmp_path / "audio.flac")


def test_failure_reports_stderr_tail(monkeypatch, fake_run: FakeRun, tmp_path: Path):
    fake_run.returncode = 1
    fake_run.stderr = "line\n" * 10 + "debate.mp4: Invalid data found"
    with pytest.raises(AudioError, match="Invalid data found"):
        extract_audio(tmp_path / "debate.mp4", tmp_path)


def test_missing_ffmpeg_gives_install_hint(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    with pytest.raises(AudioError, match="brew install ffmpeg"):
        extract_audio(tmp_path / "debate.mp4", tmp_path)


def test_unreadable_duration_raises(fake_run: FakeRun, tmp_path: Path):
    fake_run.duration = "N/A"
    with pytest.raises(AudioError, match="duración"):
        probe_duration(tmp_path / "audio.flac")


def test_purge_audio_removes_file(tmp_path: Path):
    path = tmp_path / "audio.flac"
    path.write_bytes(b"x")
    purge_audio(audio.AudioInfo(path=str(path), duration=1, sample_rate=16000))
    assert not path.exists()
    # Purging twice is harmless.
    purge_audio(audio.AudioInfo(path=str(path), duration=1, sample_rate=16000))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no instalado")
def test_real_ffmpeg_roundtrip(tmp_path: Path):
    video = tmp_path / "tono.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2", "-shortest", str(video),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    info = extract_audio(video, tmp_path)
    assert info.duration == pytest.approx(2, abs=0.1)
