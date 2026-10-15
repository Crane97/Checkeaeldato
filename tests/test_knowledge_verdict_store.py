"""Registro de veredictos en verif.claims y su uso por `conocimiento impacto`."""

import pytest

from pipeline.knowledge.impact import dependents
from pipeline.knowledge.verdict_store import record_verdicts, verdict_rows
from pipeline.models import ClaimEvidence, DetectedClaim, EvidenceItem, Source, Verdict

from .test_knowledge_schema import _observation, db  # noqa: F401  (fixture)

CLAIM = DetectedClaim(
    id="c1", speaker="A", text_original="El SMI de 2018 fue 735,90 euros",
    text_normalized="El SMI mensual de 2018 fue de 735,90 euros", claim_type="estadistica",
)  # fmt: skip
LOCAL = EvidenceItem(
    id="E1",
    source=Source(
        url="https://www.boe.es/x", title="SMI", organization="BOE", kind="indicador_local"
    ),
    excerpt="…",
    observation_ids=[7, 8],
)
WEB = EvidenceItem(
    id="E2", source=Source(url="https://x.es", title="x", organization="x", kind="web"), excerpt="…"
)
VERDICT = Verdict(
    claim_id="c1", verdict="verdadero", confidence=0.9, explanation="Coincide.",
    sources=[LOCAL.source], evidence_used=["E1"],
)  # fmt: skip


def test_rows_keep_only_the_cited_evidence_and_its_observations():
    [row] = verdict_rows(
        "job1", [CLAIM], [VERDICT], [ClaimEvidence(claim_id="c1", items=[LOCAL, WEB])]
    )
    assert row["observation_ids"] == [7, 8]
    assert [e["id"] for e in row["evidencia"]] == ["E1"]
    assert row["claim_normalizado"] == CLAIM.text_normalized


@pytest.mark.supabase
def test_recorded_verdicts_are_found_by_impact_and_replaced_on_rerun(db):  # noqa: F811
    obs = _observation(db)
    local = LOCAL.model_copy(update={"observation_ids": [obs]})
    rows = verdict_rows("job1", [CLAIM], [VERDICT], [ClaimEvidence(claim_id="c1", items=[local])])
    assert record_verdicts(db, "job1", rows, model="m", prompt_versions={"p": "h"}) == 1
    assert record_verdicts(db, "job1", rows, model="m", prompt_versions={"p": "h"}) == 1
    [found] = dependents(db, obs)
    assert (found.job_id, found.claim_id, found.veredicto) == ("job1", "c1", "verdadero")
