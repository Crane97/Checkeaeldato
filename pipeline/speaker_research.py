"""Investigación del contexto de cada orador: partido y cargo en la fecha del discurso.

Segunda etapa sobre los oradores (la primera, `speakers`, decide cuántos interlocutores hay
y lo que el propio texto dice de ellos). Para cada orador con nombre y sin partido conocido
se busca primero en fuentes oficiales (Congreso, Senado, Gobierno, BOE…) y, solo si no lo
aclaran, en la web general. Claude lee los resultados y cita frases literales; el sistema
acepta el partido solo si esas frases están en los resultados y nombran a la persona y al
partido. Lo que no se respalda queda como desconocido.
"""

import datetime as dt
import logging
import re
from collections.abc import Callable

from pydantic import BaseModel

from pipeline.governments import Governments
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import EvidenceItem, Source, Speaker, SpeakerMap, SpeakerSource
from pipeline.politicians import (
    Membership,
    Politician,
    PoliticianDirectory,
    PoliticianSource,
    Role,
    match,
    slug,
)
from pipeline.speakers import MIN_CONFIDENCE, StructuredLLM, _fold

logger = logging.getLogger(__name__)

PROMPT = "investigacion_oradores.md"
MAX_ITEMS = 6
EXCERPT_CHARS = 1500

# query, official -> (results, errors)
SpeakerSearch = Callable[..., tuple[list[EvidenceItem], list[str]]]

_HONORIFICS_RE = re.compile(
    r"^(?:(?:sr|sra|srta|d|dña|dr|dra)\.?|señor|señora|don|doña)\s+", re.IGNORECASE
)
_NAME_WORD_RE = re.compile(r"\w{3,}")
_NAME_CONNECTORS = {"del", "las", "los"}


# Schema of Claude's answer (see prompts/investigacion_oradores.md).
class ResearchAnswer(BaseModel):
    full_name: str
    party: str
    role: str
    cited: list[str]
    quotes: list[str]
    confidence: float
    notes: str


def _plain_name(name: str) -> str:
    return _HONORIFICS_RE.sub("", name.strip()).strip()


def research_query(name: str, speech_date: dt.date) -> str:
    return f"{_plain_name(name)} partido político cargo {speech_date.year}"


def _surnames(name: str) -> list[str]:
    return [w for w in _NAME_WORD_RE.findall(_fold(_plain_name(name))) if w not in _NAME_CONNECTORS]


def _format(speaker: Speaker, items: list[EvidenceItem], speech_date: dt.date) -> str:
    said = "; ".join(x for x in (speaker.role, speaker.party) if x) or "nada"
    results = "\n".join(
        f'<resultado id="{i.id}" tipo="{i.source.kind}" organismo="{i.source.organization}" '
        f'fecha="{i.source.date or "sin fecha"}" url="{i.source.url}">\n'
        f"{i.excerpt[:EXCERPT_CHARS]}\n</resultado>"
        for i in items
    )
    return (
        f"<fecha>{speech_date.isoformat()}</fecha>\n"
        f"<persona>{_plain_name(speaker.name or '')}</persona>\n"
        f"<segun_el_debate>{said}</segun_el_debate>\n"
        f"<resultados>\n{results}\n</resultados>"
    )


def _validated(
    answer: ResearchAnswer, speaker: Speaker, items: list[EvidenceItem], governments: Governments
) -> tuple[str, str | None, str | None, list[EvidenceItem], list[str]] | None:
    """(party, full name, role, cited items, quotes) if the results support the party."""
    if answer.confidence < MIN_CONFIDENCE or not answer.party.strip():
        return None
    cited = [i for i in items if i.id in set(answer.cited)]
    texts = [_fold(i.excerpt) for i in cited]
    quotes = [q for q in answer.quotes if q.strip() and any(_fold(q) in t for t in texts)]
    if not quotes:
        return None
    support = _fold(" ".join(quotes))
    surnames = _surnames(speaker.name or "")
    # Every name word must appear in the cited pages and some in the quotes ("Feijóo,
    # presidente del…"); otherwise the results may be about a namesake.
    if not all(s in " ".join(texts) for s in surnames) or not any(s in support for s in surnames):
        return None

    canonical = governments.canonical_party(answer.party)
    if canonical:
        known = governments.knowledge.parties[canonical]
        if not any(_fold(n) in support for n in {canonical, known.name, *known.aliases}):
            return None
        party = canonical
    elif _fold(answer.party) in support:
        party = answer.party.strip()
    else:
        return None

    full_name = answer.full_name.strip() or None
    if full_name and not all(s in _fold(full_name) for s in _surnames(speaker.name or "")):
        full_name = None
    return party, full_name, answer.role.strip() or None, cited, quotes


def _ask(
    speaker: Speaker,
    items: list[EvidenceItem],
    *,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
    governments: Governments,
) -> Speaker | None:
    numbered = [i.model_copy(update={"id": f"R{n}"}) for n, i in enumerate(items, 1)]
    answer = llm.structured(
        step="speaker_research",
        model=model,
        prompt=PROMPT,
        user_content=_format(speaker, numbered, speech_date),
        output_type=ResearchAnswer,
    )
    found = _validated(answer, speaker, numbered, governments)
    if found is None:
        return None
    party, full_name, role, cited, quotes = found
    kind: SpeakerSource = "oficial" if any(i.source.kind == "oficial" for i in cited) else "web"
    return speaker.model_copy(
        update={
            "name": full_name or speaker.name,
            "party": party,
            "role": speaker.role or role,
            "confidence": max(speaker.confidence, min(answer.confidence, 0.95)),
            "evidence": [*speaker.evidence, *quotes],
            "sources": [*speaker.sources, kind],
            "references": [i.source for i in cited],
        }
    )


def _source(s: PoliticianSource) -> Source:
    kind = "oficial" if s.tipo == "primaria" else "web"
    return Source(url=s.url, title=s.titulo, organization=s.organismo, date=s.fecha, kind=kind)


def _role_text(roles: list[Role]) -> str | None:
    if not roles:
        return None
    texts = [r.cargo + (" (en funciones)" if r.en_funciones else "") for r in roles]
    return "; ".join(dict.fromkeys(texts))


def _from_directory(
    speaker: Speaker, known: Politician, speech_date: dt.date, governments: Governments
) -> Speaker:
    """Fill what the base knows on the speech date; what the text said is kept."""
    membership = known.party_on(speech_date)
    roles = known.roles_on(speech_date)
    if membership is None and not roles:
        return speaker
    used = [m.source for m in ([membership] if membership else [])] + [r.source for r in roles]
    party = speaker.party
    if party is None and membership is not None:
        party = governments.canonical_party(membership.partido) or membership.partido
    facts = "; ".join(
        x for x in (membership.partido if membership else None, _role_text(roles)) if x
    )
    return speaker.model_copy(
        update={
            "name": known.nombre,
            "party": party,
            "role": speaker.role or _role_text(roles),
            "confidence": max(speaker.confidence, 0.9 if known.estado == "aprobado" else 0.8),
            "evidence": [*speaker.evidence, f"Base de políticos ({known.estado}): {facts}"],
            "sources": [*speaker.sources, "base_politicos"],
            "references": [
                *speaker.references,
                *(_source(s) for s in {s.url: s for s in used}.values()),
            ],
        }
    )


def _as_politician(speaker: Speaker, alias: str, speech_date: dt.date) -> Politician | None:
    """An automatic entry from a research result (party and role seen on the speech date)."""
    if not speaker.name or not speaker.party or not speaker.references:
        return None
    ref = speaker.references[0]
    source = PoliticianSource(
        organismo=ref.organization,
        titulo=ref.title,
        url=ref.url,
        fecha=ref.date,
        tipo="primaria" if ref.kind == "oficial" else "secundaria",
    )
    return Politician(
        id=slug(speaker.name),
        nombre=speaker.name,
        alias=[a for a in {alias} if a != speaker.name],
        origen="investigacion",
        estado="automatico",
        parties=[Membership(partido=speaker.party, constatado_en=speech_date, source=source)],
        roles=(
            [Role(cargo=speaker.role, constatado_en=speech_date, source=source)]
            if speaker.role
            else []
        ),
    )


def research_speakers(
    speakers: SpeakerMap,
    *,
    speech_date: dt.date,
    search: SpeakerSearch | None,
    llm: StructuredLLM,
    model: str,
    governments: Governments,
    directory: PoliticianDirectory | None = None,
) -> SpeakerMap:
    """Fill in the party (and role, full name) of named speakers whose party is unknown:
    first from the politicians base (no searches), then by researching the web."""
    result: list[Speaker] = []
    errors = list(speakers.errors)
    known_people = directory.usable() if directory is not None else []
    for speaker in speakers.speakers:
        if not speaker.name or speaker.label == "autor":
            result.append(speaker)
            continue
        known = match(speaker.name, known_people)
        if known is not None:
            speaker = _from_directory(speaker, known, speech_date, governments)
        if speaker.party or search is None:
            result.append(speaker)
            continue
        query = research_query(speaker.name, speech_date)
        found: Speaker | None = None
        items: list[EvidenceItem] = []
        try:
            for official in (True, False):
                new, search_errors = search(query, official=official)
                errors += [f"{speaker.name}: {e}" for e in search_errors]
                if not new:
                    continue
                items = (items + new)[:MAX_ITEMS]
                found = _ask(
                    speaker,
                    items,
                    speech_date=speech_date,
                    llm=llm,
                    model=model,
                    governments=governments,
                )
                if found:
                    break
        except LLMConfigError:
            raise
        except LLMError as exc:
            logger.warning("No se pudo investigar a %s: %s", speaker.name, exc)
            errors.append(f"{speaker.name}: {exc}")
        if found is not None and directory is not None:
            entry = _as_politician(found, speaker.name, speech_date)
            if entry is not None:
                directory.record(entry)
        result.append(found or speaker)
    errors += [e for e in getattr(directory, "errors", []) if e not in errors]
    return SpeakerMap(speakers=result, errors=errors)
