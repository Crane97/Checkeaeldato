"""Entrada de texto plano (.txt/.md) como alternativa al vídeo."""

import logging
import re
from pathlib import Path

from pipeline.models import Segment, Transcript

logger = logging.getLogger(__name__)

TEXT_SPEAKER = "autor"
SUPPORTED_SUFFIXES = (".txt", ".md")

_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")


class InputError(RuntimeError):
    """Unusable input file; the message is shown to the user."""


def _decode(raw: bytes, path: Path) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        logger.warning("%s no está en UTF-8; se lee como Windows-1252", path.name)
        return raw.decode("cp1252", errors="replace")


def load_text(path: Path) -> Transcript:
    """Read an article as a transcript: one segment per paragraph, no timestamps.

    Paragraph text is kept verbatim (apart from trimming) so claim quotes can be
    found in it later.
    """
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise InputError(f"Formato no admitido: {path.name}. Usa un archivo .txt o .md")

    text = _decode(path.read_bytes(), path).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK_RE.split(text)]
    segments = [Segment(speaker=TEXT_SPEAKER, text=p) for p in paragraphs if p]
    if not segments:
        raise InputError(f"El archivo {path.name} está vacío")

    return Transcript(provider="texto", language="es", segments=segments)
