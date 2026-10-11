"""Entrada de texto plano (.txt/.md) como alternativa al vídeo."""

import logging
import re
from pathlib import Path

from pipeline.models import Segment, Transcript

logger = logging.getLogger(__name__)

TEXT_SPEAKER = "autor"
SUPPORTED_SUFFIXES = (".txt", ".md")

_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")

# "Nombre: texto" at the start of a line, optionally as a list item or in **bold**.
_LABEL_RE = re.compile(
    r"^\s*(?:[-*•]\s+)?(?:\*\*|__)?(?P<name>[^\W\d_][\w.'’ -]{0,59}?)(?:\*\*|__)?\s*:"
    r"(?:\*\*|__)?\s+(?P<text>\S.*)$"
)
# Lowercase words allowed inside a name ("Ministra de Hacienda", "Presidente del Gobierno").
_NAME_CONNECTORS = {"de", "del", "la", "las", "los", "el", "y", "e", "i", "da", "van", "von"}
# Document labels that look like "Nombre:" but are not people.
_NOT_SPEAKERS = {
    "fuente",
    "fuentes",
    "nota",
    "notas",
    "título",
    "titulo",
    "fecha",
    "lugar",
    "resumen",
    "conclusión",
    "conclusion",
    "conclusiones",
    "introducción",
    "introduccion",
    "referencias",
    "ejemplo",
    "ejemplos",
    "datos",
    "contexto",
    "actualización",
    "actualizacion",
    "foto",
    "imagen",
    "vídeo",
    "video",
    "hora",
    "pie",
    "tema",
    "asunto",
    "importante",
    "atención",
    "crónica",
    "cronica",
    "análisis",
    "analisis",
    "opinión",
    "opinion",
    "editorial",
    "transcripción",
    "transcripcion",
    "debate",
    "entrevista",
    "noticia",
    "titular",
    "subtítulo",
    "subtitulo",
    "entradilla",
    "sumario",
    "claves",
    "cifras",
    "gráfico",
    "grafico",
    "tabla",
    "mapa",
    "última hora",
    "ultima hora",
    "en directo",
    "directo",
}
MAX_NAME_WORDS = 6


class InputError(RuntimeError):
    """Unusable input file; the message is shown to the user."""


def _decode(raw: bytes, path: Path) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        logger.warning("%s no está en UTF-8; se lee como Windows-1252", path.name)
        return raw.decode("cp1252", errors="replace")


def _speaker_label(line: str) -> tuple[str, str] | None:
    """(name, text) if the line starts with a plausible "Nombre:" speaker label."""
    match = _LABEL_RE.match(line)
    if match is None:
        return None
    name = match["name"].strip()
    words = name.split()
    if not 0 < len(words) <= MAX_NAME_WORDS or name.casefold() in _NOT_SPEAKERS:
        return None
    if not words[0][0].isupper():
        return None
    if any(not (w[0].isupper() or w[0].isdigit() or w in _NAME_CONNECTORS) for w in words):
        return None  # "El ministro dijo lo siguiente: …" is prose, not a label
    return name, match["text"].strip()


def _document_label(line: str) -> bool:
    """True for "Nota: …", "Crónica: …": the narrator speaks, not the last speaker."""
    match = _LABEL_RE.match(line)
    return match is not None and match["name"].strip().casefold() in _NOT_SPEAKERS


def _labelled_segments(text: str) -> list[Segment] | None:
    """Segments per speaker turn if the text is a dialogue ("Nombre: …" lines), else None.

    Transcripts (e.g. the Diario de Sesiones) label only the first paragraph of each
    intervention, so unlabelled lines and paragraphs continue the current turn, each paragraph
    as its own segment. Text before the first label and after a document label ("Nota:",
    "Crónica:") belongs to the author. The same name written in another case ("ANA PÉREZ" /
    "Ana Pérez") is one speaker.
    """
    lines = text.split("\n")
    labels = [label for line in lines if (label := _speaker_label(line))]
    if len(labels) < 2:
        return None

    canonical: dict[str, str] = {}
    segments: list[Segment] = []
    speaker, buffer = TEXT_SPEAKER, []

    def flush() -> None:
        joined = "\n".join(buffer).strip()
        if joined:
            segments.append(Segment(speaker=speaker, text=joined))
        buffer.clear()

    for line in lines:
        label = _speaker_label(line)
        if label is not None:
            flush()
            name, rest = label
            speaker = canonical.setdefault(name.casefold(), name)
            buffer.append(rest)
        elif not line.strip():
            flush()  # a new paragraph of the same turn
        elif _document_label(line):
            flush()
            speaker = TEXT_SPEAKER
            buffer.append(line.strip())
        else:
            buffer.append(line.strip())
    flush()
    return segments


def load_text(path: Path) -> Transcript:
    """Read an article or a dialogue as a transcript, with no timestamps.

    A dialogue written as "Nombre: texto" lines gets one segment per turn and speaker;
    any other text gets one segment per paragraph, all by the author. Text is kept
    verbatim (apart from trimming and removing the labels) so claim quotes can be
    found in it later.
    """
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise InputError(f"Formato no admitido: {path.name}. Usa un archivo .txt o .md")

    text = _decode(path.read_bytes(), path).replace("\r\n", "\n").replace("\r", "\n")
    segments = _labelled_segments(text)
    if segments is None:
        paragraphs = [p.strip() for p in _PARAGRAPH_BREAK_RE.split(text)]
        segments = [Segment(speaker=TEXT_SPEAKER, text=p) for p in paragraphs if p]
    if not segments:
        raise InputError(f"El archivo {path.name} está vacío")

    return Transcript(provider="texto", language="es", segments=segments)
