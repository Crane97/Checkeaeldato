"""Selección de los pasajes de una página relevantes para una afirmación.

Las páginas oficiales son largas (tablas, notas de prensa, PDF) y el dato que decide una
afirmación suele estar lejos del principio. En lugar de cortar la página, se puntúan sus
párrafos según las palabras clave y cifras de la afirmación y se envían los mejores.
"""

import re

from pipeline.models import DetectedClaim

EXCERPT_CHARS = 3500
SEPARATOR = " […] "
# Long paragraphs are cut into pieces of about this size so one block cannot take the budget.
CHUNK_CHARS = 400
MIN_CHUNK_CHARS = 20

_STOPWORDS = {
    "a",
    "al",
    "algo",
    "ante",
    "aquí",
    "como",
    "con",
    "cual",
    "cuando",
    "de",
    "del",
    "desde",
    "donde",
    "durante",
    "el",
    "ella",
    "ellos",
    "en",
    "entre",
    "era",
    "es",
    "esa",
    "ese",
    "eso",
    "esta",
    "está",
    "este",
    "esto",
    "estos",
    "fue",
    "ha",
    "han",
    "hasta",
    "hay",
    "la",
    "las",
    "le",
    "les",
    "lo",
    "los",
    "más",
    "mas",
    "muy",
    "ni",
    "no",
    "nos",
    "o",
    "otra",
    "otro",
    "para",
    "pero",
    "por",
    "que",
    "qué",
    "se",
    "según",
    "ser",
    "si",
    "sí",
    "sin",
    "sobre",
    "son",
    "su",
    "sus",
    "también",
    "tiene",
    "tienen",
    "todo",
    "todos",
    "tras",
    "un",
    "una",
    "unos",
    "unas",
    "y",
    "ya",
    "año",
    "años",
    "datos",
    "dato",
    "cifra",
    "total",
}
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_NUMBER_RE = re.compile(r"\d[\d.,]*")


def claim_terms(claim: DetectedClaim) -> set[str]:
    """Keywords (4+ letters, no stopwords) and numbers from the claim and its queries."""
    return query_terms(" ".join([claim.text_normalized, *claim.search_queries]))


def query_terms(text: str) -> set[str]:
    """Keywords (4+ letters, no stopwords) and numbers from a free text."""
    text = text.lower()
    terms = {w for w in _WORD_RE.findall(text) if len(w) >= 4 and w not in _STOPWORDS}
    terms |= {n.strip(".,") for n in _NUMBER_RE.findall(text)}
    return {t for t in terms if t}


def _chunks(raw: str) -> list[str]:
    pieces: list[str] = []
    for paragraph in raw.split("\n"):
        paragraph = paragraph.strip()
        while len(paragraph) > CHUNK_CHARS:
            cut = paragraph.rfind(". ", 0, CHUNK_CHARS)
            cut = cut + 1 if cut > CHUNK_CHARS // 3 else CHUNK_CHARS
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if len(paragraph) >= MIN_CHUNK_CHARS:
            pieces.append(paragraph)
    return pieces


def _score(chunk: str, terms: set[str]) -> int:
    """Words count once (substring, so "autónomas" matches "Autónomas,"); numbers count
    double but only as whole numbers, so "2" does not match "12" or "2024"."""
    lowered = chunk.lower()
    numbers = {n.strip(".,") for n in _NUMBER_RE.findall(lowered)}
    return sum(
        (2 if t in numbers else 0) if t[0].isdigit() else (1 if t in lowered else 0) for t in terms
    )


def relevant_excerpt(
    snippet: str, raw: str | None, terms: set[str], max_chars: int = EXCERPT_CHARS
) -> str:
    """Tavily's snippet plus the page passages that best match `terms`, in page order."""
    snippet = snippet.strip()
    if not raw:
        return snippet[:max_chars].strip()

    chunks = _chunks(raw)
    ranked = sorted(
        ((score, i) for i, c in enumerate(chunks) if (score := _score(c, terms)) > 0),
        key=lambda pair: (-pair[0], pair[1]),
    )
    budget = max_chars - len(snippet)
    chosen: list[int] = []
    for _, i in ranked:
        cost = len(chunks[i]) + len(SEPARATOR)
        if cost <= budget and chunks[i] not in snippet:
            chosen.append(i)
            budget -= cost
    parts = [snippet] if snippet else []
    parts += [chunks[i] for i in sorted(chosen)]
    return SEPARATOR.join(parts)[:max_chars]
