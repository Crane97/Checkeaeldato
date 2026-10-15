"""Registro de veredictos en verif.claims (trazabilidad, fase E).

Cada veredicto guarda la afirmación normalizada, las fuentes que usó y los ids de las
observaciones de las series locales citadas. Si un dato resulta erróneo o se revisa,
`uv run conocimiento impacto <observation_id>` lista los veredictos afectados. Al recalcular
un trabajo, sus filas se sustituyen (no son datos de referencia, son su resultado).
"""

import json
from collections.abc import Sequence

import psycopg

from pipeline.knowledge.coverage import topic_of
from pipeline.knowledge.on_demand import API_TITLE
from pipeline.models import ClaimEvidence, DetectedClaim, SpeakerMap, Verdict


def verdict_rows(
    job_id: str,
    claims: Sequence[DetectedClaim],
    verdicts: Sequence[Verdict],
    evidence: Sequence[ClaimEvidence],
    *,
    speakers: SpeakerMap | None = None,
    indicator_topics: dict[str, str] | None = None,
    topics: dict[str, list[str]] | None = None,
) -> list[dict]:
    """One row per verdict, with the evidence it cites (E# ids) and their observation ids,
    who said it (speaker and party as identified by the analysis) and its topic."""
    by_claim = {c.id: c for c in claims}
    people = {s.label: s for s in (speakers.speakers if speakers else [])}
    items = {e.claim_id: {i.id: i for i in e.items} for e in evidence}
    rows = []
    for v in verdicts:
        claim = by_claim.get(v.claim_id)
        if claim is None:
            continue
        used = [items.get(v.claim_id, {}).get(e) for e in v.evidence_used]
        used = [i for i in used if i is not None]
        rows.append(
            {
                "job_id": job_id,
                "claim_id": v.claim_id,
                "claim_normalizado": claim.text_normalized,
                "veredicto": v.verdict,
                "evidencia": [
                    {
                        "id": i.id,
                        "tipo": i.source.kind,
                        "organismo": i.source.organization,
                        "titulo": i.source.title,
                        "url": i.source.url,
                    }
                    for i in used
                ],
                "observation_ids": sorted({o for i in used for o in i.observation_ids}),
                # Rests on a series: the local base (observation ids) or the official API.
                "con_datos": any(
                    i.observation_ids or i.source.title.endswith(API_TITLE) for i in used
                ),  # fmt: skip
                "orador": (people[claim.speaker].name if claim.speaker in people else None)
                or claim.speaker,
                "partido": people[claim.speaker].party if claim.speaker in people else None,
                "tema": topic_of(
                    claim.text_normalized,
                    (indicator_topics or {}).get(claim.indicator or ""),
                    topics or {},
                ),
            }
        )
    return rows


def record_verdicts(
    conn: psycopg.Connection,
    job_id: str,
    rows: Sequence[dict],
    *,
    model: str,
    prompt_versions: dict[str, str],
) -> int:
    """Replace the job's rows (in the caller's transaction); returns how many were stored."""
    conn.execute("delete from verif.claims where job_id = %s", (job_id,))
    for r in rows:
        conn.execute(
            "insert into verif.claims (job_id, claim_id, claim_normalizado, veredicto,"
            " evidencia, observation_ids, modelo, version_prompts, orador, partido, tema,"
            " con_datos) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                r["job_id"],
                r["claim_id"],
                r["claim_normalizado"],
                r["veredicto"],
                json.dumps(r["evidencia"], ensure_ascii=False),
                r["observation_ids"],
                model,
                json.dumps(prompt_versions),
                r.get("orador"),
                r.get("partido"),
                r.get("tema"),
                r.get("con_datos", False),
            ),
        )
    return len(rows)
