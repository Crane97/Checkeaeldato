"""Identificación de oradores: etiqueta de transcripción → nombre, partido y rol.

Orden de fuentes: datos del usuario (speakers.json / formulario web), presentaciones y
vocativos del propio texto (interpretados por Claude y comprobados con citas literales) y
la tabla de gobiernos (partido de quien es o fue presidente). Nunca se inventa: lo que no
se puede respaldar con el texto o con datos del usuario queda como desconocido.
"""

import datetime as dt
import logging
import re
import unicodedata
from typing import Protocol, TypeVar

from pydantic import BaseModel

from pipeline.governments import Governments
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import Speaker, SpeakerInfo, SpeakerMap, Transcript

logger = logging.getLogger(__name__)

PROMPT = "identificacion_oradores.md"
MIN_CONFIDENCE = 0.6
EXCERPT_CHARS = 12000
# Turns likely to introduce or address someone.
_CUES_RE = re.compile(
    r"señor|señora|presidente|presidenta|candidat|ministr|portavoz|moderador|bienvenid|"
    r"tiene la palabra|le doy la palabra|turno de|secretari|líder|diputad|alcalde",
    re.IGNORECASE,
)
_DIARIZATION_LABEL_RE = re.compile(r"^speaker_(\d+|desconocido)$", re.IGNORECASE)
_HONORIFIC_RE = re.compile(r"^(?:(?:sr|sra|d|dña)\.?|señor|señora|don|doña)\s+", re.IGNORECASE)
_PRESIDENT_ROLE_RE = re.compile(r"president[ea] del gobierno", re.IGNORECASE)

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    def structured(
        self, *, step: str, model: str, prompt: str, user_content: str, output_type: type[T]
    ) -> T: ...


# Schema of Claude's answer (see prompts/identificacion_oradores.md).
class SpeakerGuess(BaseModel):
    label: str
    name: str
    party: str
    role: str
    evidence: list[str]
    confidence: float


class SpeakerIdentification(BaseModel):
    speakers: list[SpeakerGuess]


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain.replace("«", '"').replace("»", '"')).strip()


def _is_name_label(label: str) -> bool:
    """Dialogue labels in text input are already names; diarization labels are not."""
    return label != "autor" and not _DIARIZATION_LABEL_RE.match(label)


def format_excerpt(transcript: Transcript, max_chars: int = EXCERPT_CHARS) -> str:
    """The opening of the transcript (where speakers are introduced) plus later turns
    with presentations or vocatives, in their original order."""
    lines = [f"[S{i}] {s.speaker}: {s.text}" for i, s in enumerate(transcript.segments)]
    chosen: set[int] = set()
    used = 0
    for i, line in enumerate(lines):
        if used + len(line) > max_chars * 0.6:
            break
        chosen.add(i)
        used += len(line) + 1
    for i, segment in enumerate(transcript.segments):
        if i in chosen or not _CUES_RE.search(segment.text):
            continue
        if used + len(lines[i]) > max_chars:
            break
        chosen.add(i)
        used += len(lines[i]) + 1
    return "\n".join(lines[i] for i in sorted(chosen))


def _canonical(party: str | None, governments: Governments) -> str | None:
    if not party or not party.strip():
        return None
    return governments.canonical_party(party) or party.strip()


def _from_info(label: str, info: SpeakerInfo, governments: Governments) -> Speaker:
    return Speaker(
        label=label,
        name=info.name or (label if _is_name_label(label) else None),
        party=_canonical(info.party, governments),
        role=info.role,
        confidence=1.0,
        evidence=["speakers.json"],
        sources=["archivo"],
    )


def _validated(guess: SpeakerGuess, full_text: str, governments: Governments) -> Speaker | None:
    """Keep only what the literal evidence supports."""
    evidence = [e for e in guess.evidence if e.strip() and _fold(e) in full_text]
    if not evidence or guess.confidence < MIN_CONFIDENCE:
        return None
    support = _fold(" ".join(evidence))

    name = guess.name.strip() or None
    if name and not any(t in support for t in re.findall(r"\w{3,}", _fold(name))):
        name = None

    party = None
    canonical = governments.canonical_party(guess.party) if guess.party.strip() else None
    if canonical:
        known = governments.knowledge.parties[canonical]
        names = {canonical, known.name, *known.aliases}
        if any(_fold(n) in support for n in names):
            party = canonical
    elif guess.party.strip() and _fold(guess.party) in support:
        party = guess.party.strip()

    role = guess.role.strip() or None
    if role and _PRESIDENT_ROLE_RE.search(role) and "president" not in support:
        role = None

    if not (name or party or role):
        return None
    return Speaker(
        label=guess.label,
        name=name,
        party=party,
        role=role,
        confidence=guess.confidence,
        evidence=evidence,
        sources=["texto"],
    )


def _same_person(speaker: Speaker, president: str, parties: list[str]) -> bool:
    """Every word of the speaker's name is in the president's, and no other party."""
    words = set(re.findall(r"\w{2,}", _fold(_HONORIFIC_RE.sub("", speaker.name or ""))))
    return words <= set(re.findall(r"\w{2,}", _fold(president))) and (
        not speaker.party or speaker.party in parties
    )


def _with_table(speaker: Speaker, speech_date: dt.date, governments: Governments) -> Speaker:
    """Full name and party of a current or former president of the Government, from the
    table ("Sánchez" -> "Pedro Sánchez", PSOE)."""
    term = None
    if speaker.name:
        term = next(iter(governments.president_terms(speaker.name)), None)
        if term and not _same_person(speaker, term.president, term.parties):
            term = None  # a namesake ("Ana Sánchez" is not "Pedro Sánchez")
        if term and speaker.name != term.president:
            speaker = speaker.model_copy(update={"name": term.president})
    if speaker.party:
        return speaker
    elif speaker.role and _PRESIDENT_ROLE_RE.search(speaker.role):
        term = governments.at(speech_date)
    if term is None or not term.parties:
        return speaker
    return speaker.model_copy(
        update={
            "name": speaker.name or term.president,
            "party": term.parties[0],
            "sources": [*speaker.sources, "tabla_gobiernos"],
        }
    )


def identify_speakers(
    transcript: Transcript,
    *,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
    governments: Governments,
    info: dict[str, SpeakerInfo] | None = None,
) -> SpeakerMap:
    info = info or {}
    labels = list(dict.fromkeys(s.speaker for s in transcript.segments))
    by_label: dict[str, Speaker] = {}
    errors: list[str] = []

    for label in labels:
        if label in info:
            by_label[label] = _from_info(label, info[label], governments)

    # "autor" (plain text) is named with --autor; the text rarely identifies its author.
    pending = [
        lb
        for lb in labels
        if lb != "autor" and not (lb in by_label and by_label[lb].name and by_label[lb].party)
    ]
    if pending:
        full_text = _fold(" ".join(s.text for s in transcript.segments))
        try:
            answer = llm.structured(
                step="speakers",
                model=model,
                prompt=PROMPT,
                user_content=(
                    f"<fecha>{speech_date.isoformat()}</fecha>\n"
                    f"<etiquetas>{', '.join(pending)}</etiquetas>\n"
                    f"<transcripcion>\n{format_excerpt(transcript)}\n</transcripcion>"
                ),
                output_type=SpeakerIdentification,
            )
        except LLMConfigError:
            raise
        except LLMError as exc:
            logger.warning("No se pudo identificar a los oradores: %s", exc)
            errors.append(str(exc))
            answer = SpeakerIdentification(speakers=[])
        for guess in answer.speakers:
            if guess.label not in pending:
                continue
            found = _validated(guess, full_text, governments)
            if found is None:
                continue
            current = by_label.get(guess.label)
            if current:  # the user's data wins; the text only fills gaps
                found = current.model_copy(
                    update={
                        "name": current.name or found.name,
                        "party": current.party or found.party,
                        "role": current.role or found.role,
                        "evidence": [*current.evidence, *found.evidence],
                        "sources": [*current.sources, "texto"],
                    }
                )
            by_label[guess.label] = found

    speakers: list[Speaker] = []
    for label in labels:
        speaker = by_label.get(label)
        if speaker is None:
            speaker = (
                Speaker(
                    label=label, name=label, confidence=1.0, sources=["texto"], evidence=[label]
                )
                if _is_name_label(label)
                else Speaker(label=label)
            )
        elif speaker.name is None and _is_name_label(label):
            speaker = speaker.model_copy(update={"name": label})
        if speaker.name and not speaker.party:
            extra = next((v for k, v in info.items() if _fold(k) == _fold(speaker.name)), None)
            if extra:
                speaker = speaker.model_copy(
                    update={
                        "party": _canonical(extra.party, governments),
                        "role": speaker.role or extra.role,
                        "sources": [*speaker.sources, "archivo"],
                    }
                )
        speakers.append(_with_table(speaker, speech_date, governments))
    return SpeakerMap(speakers=speakers, errors=errors)


def parse_speaker_lines(text: str) -> dict[str, SpeakerInfo]:
    """Web form format, one speaker per line: "Nombre o etiqueta | Partido | Rol"."""
    info: dict[str, SpeakerInfo] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            continue
        key = parts[0]
        info[key] = SpeakerInfo(
            name=None if _DIARIZATION_LABEL_RE.match(key) else key,
            party=(parts[1] if len(parts) > 1 else "") or None,
            role=(parts[2] if len(parts) > 2 else "") or None,
        )
    return info
