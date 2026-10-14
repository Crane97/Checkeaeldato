from pathlib import Path

import pytest

from pipeline.neutrality import neutrality_problems

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Esto es una mentira.", "palabra prohibida «mentira»"),
        ("El candidato MIENTE.", "palabra prohibida «MIENTE»"),
        ("Según el PSOE, el paro bajó.", "nombre de partido «PSOE»"),
        ("Un portavoz del Partido Popular dijo...", "nombre de partido «Partido Popular»"),
        ("Fuentes de Vox indican...", "nombre de partido «Vox»"),
    ],
)
def test_detects_problems(text, expected):
    assert expected in neutrality_problems(text)


def test_capitalised_verb_is_a_known_false_positive():
    # A verb at sentence start matches a party name; prompts should rephrase it.
    assert neutrality_problems("Podemos comprobar la cifra con el INE.")


@pytest.mark.parametrize(
    "text",
    [
        "Si no se puede sumar la evidencia, responde no_verificable.",
        "Mientras crecía la población, podemos ver que el app de pp no cuenta.",
        "Describe hechos; no juzgues intenciones.",
    ],
)
def test_neutral_text_passes(text):
    assert neutrality_problems(text) == []


@pytest.mark.parametrize("prompt", sorted(PROMPTS_DIR.glob("*.md")), ids=lambda p: p.name)
def test_every_prompt_is_neutral(prompt: Path):
    assert neutrality_problems(prompt.read_text(encoding="utf-8")) == []
