"""Resolución de contexto político: "ustedes", "este Gobierno", "hace diez años"…

Claude solo interpreta a quién o a qué momento se refiere la frase (orador, interlocutor,
un partido o presidente nombrados, el Gobierno actual o el anterior, la audiencia). El
código calcula los periodos con la tabla de gobiernos, acota con las pistas de la frase,
reescribe la afirmación con fechas explícitas y añade los años a las búsquedas. Si no se
puede resolver con confianza, la afirmación queda como no verificable con el motivo.
"""

import datetime as dt
import logging
import re
import unicodedata
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from pipeline.governments import Governments
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import (
    ClaimDetection,
    DateRange,
    DetectedClaim,
    Speaker,
    SpeakerMap,
    Transcript,
)

logger = logging.getLogger(__name__)

PROMPT = "resolucion_contexto.md"
MIN_CONFIDENCE = 0.6
WINDOW_CHARS = 2500
MAX_QUERIES = 4

T = TypeVar("T", bound=BaseModel)

Referent = Literal[
    "ninguno",
    "orador",
    "interlocutor",
    "partido",
    "presidente",
    "gobierno_actual",
    "gobierno_anterior",
    "audiencia",
    "momento",
    "desconocido",
]


class StructuredLLM(Protocol):
    def structured(
        self, *, step: str, model: str, prompt: str, user_content: str, output_type: type[T]
    ) -> T: ...


# Schema of Claude's answer (see prompts/resolucion_contexto.md).
class ContextAnswer(BaseModel):
    referent: Referent
    addressee: str  # label or name of the addressed speaker (referent "interlocutor")
    party: str  # party as named in the text (referent "partido")
    president: str  # president as named in the text (referent "presidente")
    current_term_only: bool  # the governing party's present term, not all its periods
    years: list[int]  # explicit years that narrow the period
    years_ago: int | None  # "hace diez años"
    confidence: float
    notes: str
    rewritten: str  # normalized claim with "{actor}" and "{periodo}" placeholders


class _Unresolved(Exception):
    """Raised inside the resolution with the reason shown to the user."""


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


def _describe_speaker(speaker: Speaker | None) -> str:
    if speaker is None or not (speaker.name or speaker.party or speaker.role):
        return "desconocido"
    parts = [speaker.display()]
    if speaker.role:
        parts.append(speaker.role)
    return ", ".join(parts)


def _find_speaker(speakers: SpeakerMap, reference: str) -> Speaker | None:
    reference = reference.strip()
    if not reference:
        return None
    exact = speakers.get(reference)
    if exact:
        return exact
    wanted = set(re.findall(r"\w+", _fold(reference))) - {"senor", "senora", "don", "dona"}
    for speaker in speakers.speakers:
        if speaker.name and wanted and wanted <= set(re.findall(r"\w+", _fold(speaker.name))):
            return speaker
    return None


def _window(transcript: Transcript, segment: int | None) -> str:
    """The claim's turn and the turns before it, up to WINDOW_CHARS."""
    if segment is None:
        return ""
    lines: list[str] = []
    used = 0
    for i in range(segment, -1, -1):
        s = transcript.segments[i]
        line = f"[S{i}] {s.speaker}: {s.text}"
        if lines and used + len(line) > WINDOW_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(reversed(lines))


def _user_content(
    claim: DetectedClaim,
    transcript: Transcript,
    speakers: SpeakerMap,
    speech_date: dt.date,
    governments: Governments,
) -> str:
    current = governments.at(speech_date)
    previous = governments.previous(speech_date)
    roster = "\n".join(f"{s.label}: {_describe_speaker(s)}" for s in speakers.speakers)
    return "\n".join(
        [
            f"<fecha>{speech_date.isoformat()}</fecha>",
            f"<orador>{claim.speaker}: {_describe_speaker(speakers.get(claim.speaker))}</orador>",
            f"<oradores>\n{roster}\n</oradores>",
            "<gobierno_en_la_fecha>"
            + (governments.describe(current, speech_date) if current else "desconocido")
            + "</gobierno_en_la_fecha>",
            "<gobierno_anterior>"
            + (governments.describe(previous, previous.end or speech_date) if previous else "—")
            + "</gobierno_anterior>",
            f"<turnos>\n{_window(transcript, claim.segment)}\n</turnos>",
            f"<cita>{claim.text_original}</cita>",
            f"<afirmacion>{claim.text_normalized}</afirmacion>",
        ]
    )


def _intersect(periods: list[DateRange], windows: list[DateRange]) -> list[DateRange]:
    result = []
    for p in periods:
        for w in windows:
            start, end = max(p.start, w.start), min(p.end, w.end)
            if start <= end:
                result.append(DateRange(start=start, end=end))
    return result


def _year_windows(answer: ContextAnswer, speech_date: dt.date, gov: Governments) -> list[DateRange]:
    years = list(answer.years)
    if answer.years_ago:
        years.append(speech_date.year - answer.years_ago)
    windows = []
    for year in sorted(set(years)):
        r = gov.year_range(year)
        if r.start <= speech_date:
            windows.append(DateRange(start=r.start, end=min(r.end, speech_date)))
    return windows


def _presidents(gov: Governments, scope: list[DateRange]) -> list[str]:
    names = [
        t.president
        for t in gov.terms
        if any(t.start <= r.end and (t.end is None or r.start < t.end) for r in scope)
    ]
    return list(dict.fromkeys(names))


def _periods_text(scope: list[DateRange]) -> str:
    return " y ".join(
        f"entre el {r.start.strftime('%d/%m/%Y')} y el {r.end.strftime('%d/%m/%Y')}" for r in scope
    )


def _resolve(
    claim: DetectedClaim,
    answer: ContextAnswer,
    speakers: SpeakerMap,
    speech_date: dt.date,
    gov: Governments,
) -> tuple[str, str, list[DateRange], list[str]]:
    """(actor, short actor for the rewrite, time scope, extra notes) or _Unresolved."""
    notes: list[str] = []
    if answer.referent == "audiencia":
        raise _Unresolved(
            "Se dirige a la audiencia, no a un partido ni a un Gobierno: no hay un actor "
            "político al que atribuir el dato."
        )
    if answer.referent == "desconocido" or answer.confidence < MIN_CONFIDENCE:
        raise _Unresolved(f"Referencia ambigua (confianza {answer.confidence:.1f}).")

    party: str | None = None
    if answer.referent == "orador":
        speaker = speakers.get(claim.speaker)
        if speaker is None or not speaker.party:
            raise _Unresolved("Se desconoce el partido de quien habla.")
        party = gov.canonical_party(speaker.party) or speaker.party
    elif answer.referent == "interlocutor":
        target = _find_speaker(speakers, answer.addressee)
        if target is None:
            raise _Unresolved(
                "No se puede saber a quién se refiere: no se ha identificado al interlocutor."
            )
        if not target.party:
            raise _Unresolved(f"Se desconoce el partido de {target.name or target.label}.")
        party = gov.canonical_party(target.party) or target.party
    elif answer.referent == "partido":
        party = gov.canonical_party(answer.party)
        if party is None:
            raise _Unresolved(f"«{answer.party}» no figura en la tabla de gobiernos.")

    windows = _year_windows(answer, speech_date, gov)

    if party is not None:
        scope = gov.party_periods(party, until=speech_date)
        if not scope:
            raise _Unresolved(f"{party} no ha gobernado según la tabla de gobiernos.")
        if answer.current_term_only:
            tenure = gov.current_party_tenure(speech_date)
            term = gov.at(speech_date)
            if term and party in term.parties and tenure:
                scope = tenure
            else:
                notes.append(
                    f"{party} no gobierna en la fecha del discurso; se usan todos sus periodos."
                )
        actor = f"{party} (gobiernos de {' y '.join(_presidents(gov, scope))})"
        short = party
    elif answer.referent == "presidente":
        terms = gov.president_terms(answer.president)
        if not terms:
            raise _Unresolved(f"«{answer.president}» no figura como presidente en la tabla.")
        scope = gov.president_periods(answer.president, until=speech_date)
        actor = f"Gobierno de {terms[0].president} ({', '.join(terms[0].parties)})"
        short = actor
    elif answer.referent == "gobierno_actual":
        term = gov.at(speech_date)
        if term is None:
            raise _Unresolved("No hay Gobierno en la tabla para la fecha del discurso.")
        scope = [DateRange(start=term.start, end=speech_date)]
        actor = short = gov.describe(term, speech_date)
    elif answer.referent == "gobierno_anterior":
        term = gov.previous(speech_date)
        if term is None or term.end is None:
            raise _Unresolved("No hay Gobierno anterior en la tabla.")
        scope = [DateRange(start=term.start, end=term.end)]
        actor = short = gov.describe(term, term.end - dt.timedelta(days=1))
    elif answer.referent == "momento":
        if not windows:
            raise _Unresolved("La frase alude a un momento sin año identificable.")
        scope = windows
        governments_then = [gov.at(r.start) for r in scope]
        actor = short = "; ".join(
            dict.fromkeys(
                gov.describe(t, r.start) for t, r in zip(governments_then, scope, strict=True) if t
            )
        )
        windows = []  # already applied
    else:
        raise _Unresolved(f"Referencia no reconocida: {answer.referent}.")

    if windows:
        narrowed = _intersect(scope, windows)
        if narrowed:
            scope = narrowed
        else:
            years = ", ".join(str(w.start.year) for w in windows)
            notes.append(
                f"Los años citados ({years}) no coinciden con esos periodos; se usan todos."
            )
    return actor, short, scope, notes


def _rewrite(
    claim: DetectedClaim, answer: ContextAnswer, short: str, scope: list[DateRange]
) -> str:
    periods = _periods_text(scope)
    template = answer.rewritten.strip()
    if "{actor}" in template or "{periodo}" in template:
        return template.replace("{actor}", short).replace("{periodo}", periods)
    base = template or claim.text_normalized
    return f"{base.rstrip('.')}. (Referido a {short}, {periods}.)"


def _queries(claim: DetectedClaim, scope: list[DateRange]) -> list[str]:
    spans = [
        str(r.start.year) if r.start.year == r.end.year else f"{r.start.year}-{r.end.year}"
        for r in scope
    ]
    base = claim.search_queries or [claim.text_normalized]
    queries = [f"{base[0]} {span}" for span in spans] + base[1:]
    return list(dict.fromkeys(queries))[:MAX_QUERIES]


def resolve_context(
    detection: ClaimDetection,
    transcript: Transcript,
    speakers: SpeakerMap,
    *,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
    governments: Governments,
) -> ClaimDetection:
    resolved: list[DetectedClaim] = []
    pending = [c for c in detection.claims if c.needs_context]
    for claim in detection.claims:
        if not claim.needs_context:
            resolved.append(claim)
            continue
        logger.info(
            "Resolviendo contexto de %s (%d/%d)", claim.id, pending.index(claim) + 1, len(pending)
        )
        try:
            answer = llm.structured(
                step="context",
                model=model,
                prompt=PROMPT,
                user_content=_user_content(claim, transcript, speakers, speech_date, governments),
                output_type=ContextAnswer,
            )
        except LLMConfigError:
            raise
        except LLMError as exc:
            logger.warning("Contexto de %s no resuelto: %s", claim.id, exc)
            resolved.append(
                claim.model_copy(
                    update={
                        "context_status": "no_resuelto",
                        "resolution_notes": f"No se pudo resolver el contexto: {exc}",
                    }
                )
            )
            continue

        if answer.referent == "ninguno":
            resolved.append(claim.model_copy(update={"resolution_notes": answer.notes or None}))
            continue
        try:
            actor, short, scope, extra = _resolve(claim, answer, speakers, speech_date, governments)
        except _Unresolved as reason:
            resolved.append(
                claim.model_copy(
                    update={
                        "context_status": "no_resuelto",
                        "resolution_confidence": answer.confidence,
                        "resolution_notes": " ".join(filter(None, [str(reason), answer.notes])),
                    }
                )
            )
            continue
        resolved.append(
            claim.model_copy(
                update={
                    "context_status": "resuelto",
                    "resolved_actor": actor,
                    "time_scope": scope,
                    "resolution_confidence": answer.confidence,
                    "resolution_notes": " ".join(filter(None, [answer.notes, *extra])),
                    "text_normalized": _rewrite(claim, answer, short, scope),
                    "search_queries": _queries(claim, scope),
                }
            )
        )
    return detection.model_copy(update={"claims": resolved})
