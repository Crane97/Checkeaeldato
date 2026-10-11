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


DEBATE = """Debate electoral de prueba.

Moderadora: Buenas noches. Empezamos con economía.
Candidata A: Gracias. El paro bajó un 15 % este año.
Y la deuda está por debajo del 100 %.

Candidato B: Eso no es así. La deuda supera el 101 % del PIB.
**Candidata A:** Los datos del Banco de España dicen otra cosa.
- Moderadora: Pasamos a vivienda.
"""


def test_speaker_labels_split_a_debate_into_turns(tmp_path: Path):
    transcript = load_text(write(tmp_path, "debate.txt", DEBATE.encode()))
    turns = [(s.speaker, s.text) for s in transcript.segments]
    assert turns == [
        ("autor", "Debate electoral de prueba."),
        ("Moderadora", "Buenas noches. Empezamos con economía."),
        (
            "Candidata A",
            "Gracias. El paro bajó un 15 % este año.\nY la deuda está por debajo del 100 %.",
        ),
        ("Candidato B", "Eso no es así. La deuda supera el 101 % del PIB."),
        ("Candidata A", "Los datos del Banco de España dicen otra cosa."),
        ("Moderadora", "Pasamos a vivienda."),
    ]


def test_same_speaker_written_differently_in_case_is_unified(tmp_path: Path):
    text = "ANA PÉREZ: Hola.\nLuis: Adiós.\nAna Pérez: Otra vez."
    speakers = [s.speaker for s in load_text(write(tmp_path, "a.txt", text.encode())).segments]
    assert speakers == ["ANA PÉREZ", "Luis", "ANA PÉREZ"]


@pytest.mark.parametrize(
    "text",
    [
        "Fuente: INE.\nNota: datos provisionales.\nEl paro bajó.",  # labels that are not people
        "Conclusión: el paro bajó un 15 %.",  # a single label is not a dialogue
        "El ministro dijo lo siguiente: que el paro bajó. Y añadió: que la deuda subió.",
    ],
)
def test_prose_and_document_labels_are_not_speakers(tmp_path: Path, text):
    speakers = {s.speaker for s in load_text(write(tmp_path, "a.txt", text.encode())).segments}
    assert speakers == {"autor"}


def test_document_label_ends_the_turn_and_returns_to_the_author(tmp_path: Path):
    text = (
        "Moderadora: Pasamos al siguiente bloque.\n"
        "Candidata A: Gracias.\n"
        "\n"
        "Crónica: el portavoz del Gobierno aseguró que la deuda superó el 100 %.\n"
    )
    segments = load_text(write(tmp_path, "a.txt", text.encode())).segments
    assert segments[-1].speaker == "autor"


def test_paragraphs_without_label_continue_the_current_turn(tmp_path: Path):
    # Parliamentary transcripts label only the first paragraph of each intervention.
    text = (
        "Sr. Pérez: Gracias, presidente.\n"
        "\n"
        "El paro subió un 3 % el último año.\n"
        "\n"
        "Presidente: Muchas gracias, señoría.\n"
        "\n"
        "Hemos creado 500.000 empleos.\n"
    )
    segments = load_text(write(tmp_path, "a.txt", text.encode())).segments
    assert [(s.speaker, s.text) for s in segments] == [
        ("Sr. Pérez", "Gracias, presidente."),
        ("Sr. Pérez", "El paro subió un 3 % el último año."),
        ("Presidente", "Muchas gracias, señoría."),
        ("Presidente", "Hemos creado 500.000 empleos."),
    ]
