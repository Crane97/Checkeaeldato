"""Esquema de los informes (migración 0011): estados, versiones de veredicto y borrado.

Corre contra el proyecto de DESARROLLO dentro de una transacción que se deshace (fixture
`db` de test_knowledge_schema).
"""

import json

import psycopg
import pytest

from .test_knowledge_schema import db  # noqa: F401  (fixture)

pytestmark = pytest.mark.supabase


def _report(conn, rid="a1b2c3d4e5f6", estado="borrador", **extra) -> str:
    fields = {
        "id": rid,
        "slug": f"2026-10-18-debate-{rid[:4]}",
        "titulo": "Debate a tres",
        "fecha_discurso": "2026-10-18",
        "input_kind": "texto",
        "fuente": "debate.txt",
        "origen": "local",
        "estado": estado,
        **extra,
    }
    cols = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    conn.execute(f"insert into app.reports ({cols}) values ({marks})", list(fields.values()))
    return rid


def _version(conn, rid, claim_id="c1", version=1, vigente=True, **extra) -> int:
    fields = {
        "report_id": rid,
        "claim_id": claim_id,
        "orden": 0,
        "kind": "afirmacion",
        "version": version,
        "vigente": vigente,
        "origen": "analisis",
        "etiqueta": "falso",
        "categoria": "falso_con_fuente_oficial",
        "orador": "Ana Ruiz",
        "contenido": json.dumps({"id": claim_id}),
        **extra,
    }
    cols = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    return conn.execute(
        f"insert into app.claim_versions ({cols}) values ({marks}) returning id",
        list(fields.values()),
    ).fetchone()[0]


REEVALUATED = {
    "origen": "reevaluacion",
    "motivo": "objeción del revisor",
    "etiqueta": "verdadero",
    "categoria": "verdadero",
}


def _fails(conn, sql, params=(), error=psycopg.errors.CheckViolation):
    conn.execute("savepoint s")
    with pytest.raises(error):
        conn.execute(sql, params)
    conn.execute("rollback to savepoint s")


def test_a_report_starts_in_analysis_and_states_carry_their_dates(db):  # noqa: F811
    rid = _report(db, estado="en_analisis")
    assert db.execute("select estado from app.reports where id = %s", (rid,)).fetchone() == (
        "en_analisis",
    )
    _fails(db, "update app.reports set estado = 'publicado' where id = %s", (rid,))  # no date
    _fails(
        db,
        "update app.reports set estado = 'retirado', retirado_en = now() where id = %s",
        (rid,),
    )  # no note
    _fails(db, "update app.reports set estado = 'error' where id = %s", (rid,))  # no message
    _fails(db, "update app.reports set slug = 'Debate a tres' where id = %s", (rid,))


def test_only_one_version_is_current_and_labels_match_the_kind(db):  # noqa: F811
    rid = _report(db)
    _version(db, rid)
    db.execute("savepoint s")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _version(db, rid, version=2, vigente=True)
    db.execute("rollback to savepoint s")
    _version(db, rid, version=2, vigente=False, **REEVALUATED)
    db.execute("savepoint s")
    with pytest.raises(psycopg.errors.CheckViolation):
        _version(db, rid, claim_id="p1", kind="promesa", etiqueta="falso", categoria=None)
    db.execute("rollback to savepoint s")
    _version(db, rid, claim_id="p1", kind="promesa", etiqueta="dudosa", categoria=None)
    db.execute("savepoint s")
    with pytest.raises(psycopg.errors.CheckViolation):
        _version(db, rid, claim_id="c9", etiqueta="engañoso")
    db.execute("rollback to savepoint s")


def test_a_version_is_immutable_except_for_which_one_is_current(db):  # noqa: F811
    rid = _report(db)
    v1 = _version(db, rid)
    v2 = _version(db, rid, version=2, vigente=False, **REEVALUATED)
    db.execute("update app.claim_versions set vigente = false where id = %s", (v1,))
    db.execute("update app.claim_versions set vigente = true where id = %s", (v2,))
    _fails(db, "update app.claim_versions set etiqueta = 'falso' where id = %s", (v2,))
    _fails(db, "update app.claim_versions set contenido = '{}' where id = %s", (v1,))
    _fails(db, "delete from app.claim_versions where id = %s", (v1,))


def test_only_drafts_and_failed_analyses_can_be_deleted_with_all_their_data(db):  # noqa: F811
    draft = _report(db)
    _version(db, draft)
    db.execute(
        "insert into app.report_files (report_id, tipo, ruta, sha256, bytes)"
        " values (%s, 'informe_html', %s, repeat('a', 64), 10)",
        (draft, f"{draft}/informe.html"),
    )
    db.execute("delete from app.reports where id = %s", (draft,))
    assert db.execute(
        "select count(*) from app.claim_versions where report_id = %s", (draft,)
    ).fetchone() == (0,)

    published = _report(
        db, rid="0123456789ab", estado="publicado", tipo="debate", publicado_en="2026-10-19"
    )
    _version(db, published)
    _fails(db, "delete from app.reports where id = %s", (published,))


def test_published_reports_need_an_event_type(db):  # noqa: F811
    db.execute("savepoint s")
    with pytest.raises(psycopg.errors.CheckViolation):
        _report(db, estado="publicado", publicado_en="2026-10-19")
    db.execute("rollback to savepoint s")


def test_the_queue_points_to_a_report_and_tracks_attempts(db):  # noqa: F811
    rid = _report(db, estado="en_analisis")
    qid = db.execute(
        "insert into app.analysis_queue (report_id, solicitud) values (%s, %s) returning id",
        (rid, json.dumps({"texto": "Hola"})),
    ).fetchone()[0]
    row = db.execute(
        "select estado, intentos from app.analysis_queue where id = %s", (qid,)
    ).fetchone()
    assert row == ("pendiente", 0)


def test_the_api_roles_cannot_reach_reports(db):  # noqa: F811
    for table in ("reports", "claim_versions", "report_files", "analysis_queue"):
        assert db.execute(
            "select relrowsecurity from pg_class where oid = %s::regclass", (f"app.{table}",)
        ).fetchone() == (True,)
        assert db.execute(
            "select has_table_privilege('anon', %s, 'select')", (f"app.{table}",)
        ).fetchone() == (False,)
