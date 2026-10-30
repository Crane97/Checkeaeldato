"""Esquema de la base de conocimiento.

Los tests marcados `supabase` corren contra el proyecto de DESARROLLO dentro de una
transacción que se deshace al final (en Postgres el DDL también es transaccional): se
aplican las migraciones desde cero y no queda nada en la base.
"""

from pathlib import Path

import psycopg
import pytest

from pipeline.knowledge.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    apply,
    load_migrations,
)

SCHEMAS = ("ref", "evid", "verif", "ops", "app")


def test_migrations_are_numbered_and_unique():
    migrations = load_migrations()
    assert [m.version for m in migrations][:2] == ["0001", "0002"]
    assert all(m.sql.strip() for m in migrations)


def test_misnamed_migration_is_rejected(tmp_path: Path):
    (tmp_path / "esquema.sql").write_text("select 1;")
    with pytest.raises(MigrationError, match="esquema.sql"):
        load_migrations(tmp_path)


def test_migrations_never_update_or_delete_observations():
    for m in load_migrations(MIGRATIONS_DIR):
        sql = m.sql.lower()
        assert "update ref.observations" not in sql and "delete from ref.observations" not in sql


@pytest.fixture
def db(supabase_url: str):
    """Fresh schema inside a transaction that is always rolled back."""
    with psycopg.connect(supabase_url, connect_timeout=20) as conn:
        try:
            for schema in SCHEMAS:
                conn.execute(f"drop schema if exists {schema} cascade")
            conn.execute(
                "create schema ops; create table ops.schema_migrations (version text primary key,"
                " nombre text not null, checksum text not null, aplicada_en timestamptz not null"
                " default now())"
            )
            for m in load_migrations():
                apply(conn, m)
            yield conn
        finally:
            conn.rollback()


def _observation(conn, **overrides) -> int:
    conn.execute(
        "insert into ref.indicators (id, nombre, definicion, unidad, frecuencia, organismo, tema,"
        " salto_max_pct) values ('smi', 'SMI', 'Salario mínimo', 'euros/mes', 'anual', 'BOE',"
        " 'empleo', 30)"
        " on conflict do nothing"
    )
    source = conn.execute(
        "insert into ref.sources (organismo, titulo, url, tipo) values"
        " ('BOE', 'RD 1077/2017', 'https://www.boe.es/x', 'primaria')"
        " on conflict (organismo, titulo, url) do update set tipo = excluded.tipo returning id"
    ).fetchone()[0]
    row = {
        "indicator_id": "smi",
        "periodo": "2018",
        "periodo_inicio": "2018-01-01",
        "periodo_fin": "2018-12-31",
        "valor": 735.90,
        "unidad": "euros/mes",
        "fecha_publicacion": "2017-12-30",
        "estado": "pendiente",
        "source_id": source,
        "version": 1,
        "vigente": True,
    } | overrides
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    return conn.execute(
        f"insert into ref.observations ({cols}) values ({marks}) returning id",
        list(row.values()),
    ).fetchone()[0]


def _fails(conn, sql: str, params=(), match: str = "") -> None:
    conn.execute("savepoint t")
    with pytest.raises(psycopg.Error, match=match):
        conn.execute(sql, params)
    conn.execute("rollback to savepoint t")


@pytest.mark.supabase
def test_all_tables_are_created(db):
    tables = {
        f"{s}.{t}"
        for s, t in db.execute(
            "select table_schema, table_name from information_schema.tables"
            " where table_schema = any(%s)",
            (list(SCHEMAS),),
        ).fetchall()
    }
    assert {
        "ref.indicators",
        "ref.territories",
        "ref.observations",
        "ref.sources",
        "ref.facts",
        "ref.governments",
        "evid.web_facts",
        "verif.claims",
        "ops.ingest_runs",
        "ops.source_state",
        "ref.observation_events",
    } <= tables


@pytest.mark.supabase
def test_observations_cannot_be_deleted_truncated_or_edited(db):
    obs = _observation(db)
    _fails(db, "delete from ref.observations where id = %s", (obs,), "inmutable")
    _fails(db, "truncate ref.observations", match="TRUNCATE")
    _fails(db, "update ref.observations set valor = 736 where id = %s", (obs,), "inmutable")
    _fails(
        db,
        "update ref.observations set fecha_publicacion = '2018-01-01' where id = %s",
        (obs,),
        "inmutable",
    )


@pytest.mark.supabase
def test_only_allowed_state_changes_and_they_are_logged(db):
    obs = _observation(db)
    db.execute("update ref.observations set estado = 'aprobado' where id = %s", (obs,))
    _fails(db, "update ref.observations set estado = 'pendiente' where id = %s", (obs,), "estado")
    db.execute("update ref.observations set vigente = false where id = %s", (obs,))
    _fails(db, "update ref.observations set vigente = true where id = %s", (obs,), "vigente")
    events = db.execute(
        "select campo, antes, despues from ref.observation_events where observation_id = %s"
        " order by id",
        (obs,),
    ).fetchall()
    assert events == [("estado", "pendiente", "aprobado"), ("vigente", "true", "false")]
    _fails(db, "delete from ref.observation_events", match="solo inserción")


@pytest.mark.supabase
def test_a_revision_is_a_new_version_and_only_one_is_current(db):
    first = _observation(db)
    # Two current versions of the same datum are not allowed…
    db.execute("savepoint dup")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _observation(db, version=2)
    db.execute("rollback to savepoint dup")
    # …so a revision first retires the previous one, then inserts the next version.
    db.execute("update ref.observations set vigente = false where id = %s", (first,))
    second = _observation(db, version=2, valor=735.9, fecha_publicacion="2018-01-20")
    rows = db.execute(
        "select id, version, vigente from ref.observations"
        " where indicator_id = 'smi' order by version"
    ).fetchall()
    assert rows == [(first, 1, False), (second, 2, True)]


@pytest.mark.supabase
def test_web_facts_need_exact_keys(db):
    _fails(
        db,
        "insert into evid.web_facts (enunciado, claves, source_url, tipo_fuente, valor, cita)"
        " values ('x', '{\"indicador\": \"smi\"}', 'https://a.es', 'medio', 1, 'cita')",
        match="check",
    )


def _run(conn) -> int:
    return conn.execute(
        "insert into ops.ingest_runs (conector) values ('ine') returning id"
    ).fetchone()[0]


@pytest.mark.supabase
def test_inserts_follow_the_version_chain(db):
    _observation(db)
    for overrides, match in (
        ({"version": 3}, "versión esperada 2"),  # a gap in the chain
        ({"version": 2, "fecha_publicacion": "2017-01-01"}, "anterior"),  # going back in time
        ({"periodo": "2019", "version": 2}, "debe ser la 1"),  # first version must be 1
    ):
        db.execute("savepoint s")
        with pytest.raises(psycopg.Error, match=match):
            _observation(db, **overrides)
        db.execute("rollback to savepoint s")


@pytest.mark.supabase
def test_only_automatic_ingestion_inserts_approved_rows(db):
    db.execute("savepoint s")
    with pytest.raises(psycopg.Error, match="pendiente"):
        _observation(db, estado="aprobado")  # hand-curated data enters as pending
    db.execute("rollback to savepoint s")
    assert _observation(db, estado="aprobado", ingest_run_id=_run(db))


@pytest.mark.supabase
def test_events_record_the_connection_role_and_the_reviewer(db):
    obs = _observation(db)
    db.execute("select set_config('app.revisor', 'Jorge', true)")
    db.execute("update ref.observations set estado = 'aprobado' where id = %s", (obs,))
    por, revisor = db.execute(
        "select por, revisor from ref.observation_events where observation_id = %s", (obs,)
    ).fetchone()
    assert por == db.execute("select session_user").fetchone()[0] and revisor == "Jorge"


@pytest.mark.supabase
def test_source_identity_and_nan_values_are_protected(db):
    _observation(db)
    _fails(db, "update ref.sources set url = 'https://otra.es'", match="identidad")
    _fails(db, "update ref.indicators set unidad = 'euros/año'", match="unidad")
    db.execute("savepoint s")
    with pytest.raises(psycopg.Error, match="check"):
        _observation(db, periodo="2020", valor=float("nan"))
    db.execute("rollback to savepoint s")


@pytest.mark.supabase
@pytest.mark.parametrize(
    "claves",
    [
        '{"indicador": "smi", "territorio": "ES", "periodo": 2018}',  # number, not text
        '{"indicador": "smi", "territorio": "ES", "periodo": "2018", "x": "y"}',  # extra key
    ],
)
def test_web_fact_keys_are_exactly_three_strings(db, claves):
    _fails(
        db,
        "insert into evid.web_facts (enunciado, claves, source_url, tipo_fuente, valor, cita)"
        " values ('x', %s::jsonb, 'https://a.es', 'medio', 1, 'cita')",
        (claves,),
        "check",
    )


@pytest.mark.supabase
def test_rls_is_enabled_on_every_table(db):
    without = db.execute(
        "select n.nspname || '.' || c.relname from pg_class c join pg_namespace n"
        " on n.oid = c.relnamespace where n.nspname in ('ref','evid','verif','ops')"
        " and c.relkind = 'r' and not c.relrowsecurity and c.relname <> 'schema_migrations'"
    ).fetchall()
    assert without == []


@pytest.mark.supabase
def test_governments_enter_pending_and_need_a_reviewer_to_be_approved(db):
    db.execute(
        "insert into ref.governments (id, nivel, territorio, presidente, partidos, inicio)"
        " values ('es-x', 'nacional', 'España', 'X', '{A}', '2018-06-02')"
    )
    assert db.execute("select estado from ref.governments where id = 'es-x'").fetchone() == (
        "pendiente",
    )
    _fails(db, "update ref.governments set estado = 'aprobado' where id = 'es-x'", match="check")
    db.execute(
        "update ref.governments set estado = 'aprobado', revisado_por = 'Jorge',"
        " revisado_en = now() where id = 'es-x'"
    )


@pytest.mark.supabase
def test_governments_json_loads_pending_and_is_idempotent(db, tmp_path):
    import json

    from pipeline.governments import DEFAULT_KNOWLEDGE_PATH
    from pipeline.knowledge.governments_load import load_governments

    first = load_governments(db)
    assert len([n for n in first.new if n.startswith("gobierno")]) == 7
    assert first.changed == []
    estados = {e for (e,) in db.execute("select estado from ref.governments").fetchall()}
    assert estados == {"pendiente"}
    partners = db.execute(
        "select partido, inicio from ref.government_partners"
        " where government_id = 'es-2018-sanchez' order by inicio"
    ).fetchall()
    assert [p for p, _ in partners] == ["Unidas Podemos", "Sumar"]

    again = load_governments(db)
    assert again.new == [] and again.changed == []

    data = json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())
    data["governments"][0]["notes"] = "otra nota"
    edited = tmp_path / "governments.json"
    edited.write_text(json.dumps(data, ensure_ascii=False))
    report = load_governments(db, edited)
    assert report.changed == [f"gobierno {data['governments'][0]['id']}"]  # warned, not overwritten


@pytest.mark.supabase
def test_update_overwrites_and_sends_approved_rows_back_to_review(db, tmp_path):
    import json

    from pipeline.governments import DEFAULT_KNOWLEDGE_PATH
    from pipeline.knowledge.governments_load import load_governments

    load_governments(db)
    gid = "es-2018-sanchez"
    db.execute(
        "update ref.governments set estado = 'aprobado', revisado_por = 'Jorge',"
        " revisado_en = now() where id = %s",
        (gid,),
    )
    data = json.loads(DEFAULT_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    next(g for g in data["governments"] if g["id"] == gid)["notes"] = "nota corregida"
    edited = tmp_path / "governments.json"
    edited.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    report = load_governments(db, edited, update=True)
    assert report.updated == [f"gobierno {gid}"]
    row = db.execute(
        "select notas, estado, revisado_por from ref.governments where id = %s", (gid,)
    ).fetchone()
    assert row == ("nota corregida", "pendiente", None)


@pytest.mark.supabase
def test_pending_rows_cannot_carry_a_reviewer_and_partners_must_be_parties(db):
    db.execute(
        "insert into ref.governments (id, nivel, territorio, presidente, partidos, inicio)"
        " values ('es-y', 'nacional', 'España', 'Y', '{A}', '2018-06-02')"
    )
    _fails(db, "update ref.governments set revisado_por = 'Jorge' where id = 'es-y'", match="check")
    _fails(
        db,
        "update ref.governments set estado = 'aprobado', revisado_por = ' ', revisado_en = now()"
        " where id = 'es-y'",
        match="check",
    )
    _fails(
        db,
        "insert into ref.government_partners (government_id, partido, inicio)"
        " values ('es-y', 'Inexistente', '2019-01-01')",
        match="foreign key",
    )


@pytest.mark.supabase
def test_catalog_syncs_and_its_identity_fields_are_locked(db):
    from pipeline.knowledge.catalog import load_catalog, sync_catalog

    catalog = load_catalog()
    first = sync_catalog(db, catalog)
    assert len(first.new) == len(catalog)
    again = sync_catalog(db, catalog)
    assert again.new == [] and again.updated == [] and len(again.unchanged) == len(catalog)

    renamed = {k: v.model_copy(update={"nombre": v.nombre + " (2)"}) for k, v in catalog.items()}
    assert len(sync_catalog(db, renamed).updated) == len(catalog)  # names may change
    db.execute("savepoint s")
    changed_unit = {"paro_epa": catalog["paro_epa"].model_copy(update={"unidad": "tanto por uno"})}
    with pytest.raises(ValueError, match="paro_epa.*unidad"):
        sync_catalog(db, changed_unit)  # the database refuses it: needs a new indicator
    db.execute("rollback to savepoint s")
    _fails(
        db,
        "update ref.indicators set salto_max_pct = 5 where id = 'paro_epa'",
        match="check",  # one threshold only: paro_epa already has salto_max_puntos
    )


@pytest.mark.supabase
def test_json_base_loads_validated_and_reloading_changes_nothing(db):
    import datetime as dt

    from pipeline.knowledge.catalog import load_catalog, sync_catalog
    from pipeline.knowledge.observations_load import load_json_base

    catalog = load_catalog()
    sync_catalog(db, catalog)
    today = dt.date(2026, 10, 14)
    first = {s.indicator_id: s for s in load_json_base(db, catalog, today=today)}
    assert all(not s.errors for s in first.values()), [s.errors for s in first.values()]
    # Official primary sources (BOE) enter approved after validation (2026-10-14).
    assert (first["smi_mensual"].new, first["smi_mensual"].pending) == (25, 0)
    assert first["paro_epa"].pending == 0 and first["paro_epa"].new > 90

    smi_2018 = db.execute(
        "select valor, fecha_publicacion, fecha_publicacion_estimada, estado"
        " from ref.observations where indicator_id = 'smi_mensual' and periodo = '2018'"
    ).fetchone()
    assert (float(smi_2018[0]), smi_2018[1], smi_2018[2], smi_2018[3]) == (
        735.9,
        dt.date(2017, 12, 30),  # the BOE publication date of RD 1077/2017
        False,
        "aprobado",
    )
    paro = db.execute(
        "select fecha_publicacion, fecha_publicacion_estimada, estado from ref.observations"
        " where indicator_id = 'paro_epa' and periodo = '2013T1'"
    ).fetchone()
    assert paro == (dt.date(2013, 4, 28), True, "aprobado")  # quarter end + 28 days

    again = load_json_base(db, catalog, today=today)
    assert sum(s.new + s.revisions for s in again) == 0
