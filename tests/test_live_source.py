from pathlib import Path

import pytest

from pipeline.audio import AudioError
from pipeline.live.source import (
    BLOCK_PATTERN,
    AudioBlock,
    FfmpegBlockSource,
    block_command,
    is_local_file,
)


class FakeProcess:
    """ffmpeg stand-in: each poll() writes the next block file; exits when they run out."""

    def __init__(self, out_dir: Path, files: int, returncode: int = 0, stderr: bytes = b""):
        self.out_dir, self.files, self.returncode_at_end = out_dir, files, returncode
        self.written = 0
        self.returncode: int | None = None
        self.terminated = False
        self.stderr = _Stream(stderr)

    def poll(self) -> int | None:
        if self.written < self.files:
            (self.out_dir / (BLOCK_PATTERN % self.written)).write_bytes(b"fLaC")
            self.written += 1
            return None
        self.returncode = self.returncode_at_end
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class _Stream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


def source(tmp_path: Path, process: FakeProcess, **kwargs) -> FfmpegBlockSource:
    kwargs.setdefault("probe", lambda path: 60.0)
    return FfmpegBlockSource(
        "https://emisora.example/directo.m3u8",
        tmp_path / "blocks",
        popen=lambda cmd, **kw: process,
        sleep=lambda s: None,
        which=lambda name: "/usr/bin/ffmpeg",
        **kwargs,
    )


def test_command_cuts_mono_16khz_flac_blocks(tmp_path):
    cmd = block_command("https://x/y.m3u8", tmp_path, 90, realtime=False)
    assert cmd[0] == "ffmpeg" and "-re" not in cmd
    assert cmd[cmd.index("-segment_time") + 1] == "90"
    assert cmd[cmd.index("-ar") + 1] == "16000" and cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[-1].endswith("block_%05d.flac")


def test_a_recorded_file_is_played_at_its_own_pace(tmp_path):
    assert "-re" in block_command("debate.mp4", tmp_path, 60, realtime=True)


@pytest.mark.parametrize("seconds", [5, 601])
def test_block_length_is_bounded(tmp_path, seconds):
    with pytest.raises(ValueError):
        block_command("x.mp4", tmp_path, seconds, realtime=True)


def test_local_files_are_detected(tmp_path):
    video = tmp_path / "debate.mp4"
    video.write_bytes(b"")
    assert is_local_file(str(video))
    assert not is_local_file("https://emisora.example/directo.m3u8")
    assert not is_local_file(str(tmp_path / "no-existe.mp4"))


def test_blocks_are_yielded_once_the_next_file_exists(tmp_path):
    process = FakeProcess(tmp_path / "blocks", files=3)
    (tmp_path / "blocks").mkdir()
    durations = iter([60.0, 60.0, 12.5])
    src = source(tmp_path, process, probe=lambda path: next(durations))

    blocks = list(src.blocks())

    assert [(b.index, b.start, b.duration) for b in blocks] == [
        (0, 0.0, 60.0),
        (1, 60.0, 60.0),
        (2, 120.0, 12.5),
    ]
    assert blocks[2].end == 132.5
    assert all(Path(b.path).exists() for b in blocks)


def test_an_ffmpeg_failure_raises_with_its_last_lines(tmp_path):
    (tmp_path / "blocks").mkdir()
    process = FakeProcess(tmp_path / "blocks", files=0, returncode=1, stderr=b"x\nInvalid data")
    with pytest.raises(AudioError, match="Invalid data"):
        list(source(tmp_path, process).blocks())


def test_blocks_before_a_failure_are_still_yielded(tmp_path):
    (tmp_path / "blocks").mkdir()
    process = FakeProcess(tmp_path / "blocks", files=2, returncode=1, stderr=b"corte")
    gen = source(tmp_path, process).blocks()
    assert next(gen).index == 0
    assert next(gen).index == 1
    with pytest.raises(AudioError):
        next(gen)


def test_stop_terminates_ffmpeg_without_raising(tmp_path):
    (tmp_path / "blocks").mkdir()
    process = FakeProcess(tmp_path / "blocks", files=5)
    src = source(tmp_path, process)
    gen = src.blocks()
    assert next(gen).index == 0
    src.stop()
    assert process.terminated
    assert [b.index for b in gen] == [1, 2, 3, 4]  # what was already cut is kept


def test_missing_ffmpeg_is_a_clear_error(tmp_path):
    src = FfmpegBlockSource("x.m3u8", tmp_path, which=lambda name: None)
    with pytest.raises(AudioError, match="ffmpeg"):
        next(src.blocks())


def test_audio_block_end():
    assert AudioBlock(index=0, path="a", start=10, duration=5).end == 15
