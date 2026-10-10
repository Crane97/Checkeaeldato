from pathlib import Path

import pytest

from pipeline.text_input import TEXT_SPEAKER, InputError, load_text


def write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_paragraphs_become_segments_without_times(tmp_path: Path):
    path = write(
        tmp_path,
        "articulo.txt",
        "El paro bajó un 20 %.\nSegún el INE.\n\n\n  La deuda subió.  \n".encode(),
    )
    transcript = load_text(path)

    assert transcript.provider == "texto"
    assert transcript.duration is None
    assert [s.text for s in transcript.segments] == [
        "El paro bajó un 20 %.\nSegún el INE.",
        "La deuda subió.",
    ]
    assert all(s.speaker == TEXT_SPEAKER for s in transcript.segments)
    assert all(s.start is None and s.end is None for s in transcript.segments)


def test_windows_line_endings_and_bom(tmp_path: Path):
    path = write(tmp_path, "a.md", "﻿Primero.\r\n\r\nSegundo.\r\n".encode())
    assert [s.text for s in load_text(path).segments] == ["Primero.", "Segundo."]


def test_falls_back_to_windows_encoding(tmp_path: Path):
    path = write(tmp_path, "a.txt", "La población creció.".encode("cp1252"))
    assert load_text(path).segments[0].text == "La población creció."


def test_empty_text_is_rejected(tmp_path: Path):
    with pytest.raises(InputError, match="vacío"):
        load_text(write(tmp_path, "a.txt", b"  \n\n "))


def test_unsupported_extension_is_rejected(tmp_path: Path):
    with pytest.raises(InputError, match=".txt o .md"):
        load_text(write(tmp_path, "a.pdf", b"%PDF"))
