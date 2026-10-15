"""`conocimiento impacto`: veredictos que dependen de un dato y cómo recalcularlos."""

import datetime as dt
import json
from pathlib import Path

import pytest

from pipeline.knowledge.impact import dependents, observation_summary, reprocess_commands

from .test_knowledge_schema import _observation, db  # noqa: F401  (fixture)


def _manifest(root: Path, job_id: str, input_path: str) -> None:
    (root / job_id).mkdir(parents=True)
    (root / job_id / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "input_path": input_path,
                "input_sha256": "x",
                "input_kind": "texto",
                "speech_date": "2023-05-10",
                "created_at": "2026-10-14T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def test_reprocess_commands_come_from_each_job_manifest(tmp_path: Path):
    _manifest(tmp_path, "abc123", "data/mitin de mayo.txt")
    commands = reprocess_commands(["abc123", "abc123", None, "gone99"], tmp_path)
    assert commands == [
        "uv run verificar 'data/mitin de mayo.txt' --fecha 2023-05-10 --from-step evidence",
        f"# gone99: no está en {tmp_path}/ (sin manifest)",
    ]


@pytest.mark.supabase
def test_dependents_are_found_through_observation_ids(db):  # noqa: F811
    used = _observation(db)
    other = _observation(db, periodo="2019", periodo_inicio="2019-01-01", periodo_fin="2019-12-31")
    for job, ids in (("j1", [used]), ("j2", [used, other]), ("j3", [other])):
        db.execute(
            "insert into verif.claims (job_id, claim_id, claim_normalizado, veredicto, evidencia,"
            " observation_ids) values (%s, 'c1', 'El SMI de 2018 fue 735,90 euros', 'verdadero',"
            " '[]', %s)",
            (job, ids),
        )
    assert sorted(d.job_id for d in dependents(db, used)) == ["j1", "j2"]
    assert "smi · 2018 · ES: 735.9 euros/mes" in observation_summary(db, used)
    assert observation_summary(db, 10**12) is None
    assert dependents(db, 10**12) == []
    assert isinstance(dependents(db, used)[0].fecha_verificacion, dt.datetime)
