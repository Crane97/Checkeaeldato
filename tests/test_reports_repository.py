"""Repositorio de informes: slug, guardar el resultado de un análisis y reconstruirlo."""

import datetime as dt

import pytest

from pipeline.models import Claim, Promise, Report, RunCost, Source, Speaker
from pipeline.reports.repository import (
    ReportError,
    create_report,
    load_meta,
    load_report,
    mark_error,
    set_progress,
    slugify,
    store_result,
)

from .test_knowledge_schema import db  # noqa: F401  (fixture)

INE = Source(url="https://www.ine.es/epa", title="EPA", organization="INE", kind="oficial")
JOB = "abc123def456"


def claim(cid: str, speaker: str, verdict: str = "verdadero") -> Claim:
    return Claim(
        id=cid,
        speaker=speaker,
        text_original="El paro bajó un 15 %",
        text_normalized="El paro bajó un 15 % entre 2022 y 2026.",
        claim_type="estadistica",
        verdict=verdict,
        confidence=0.0 if verdict == "no_verificable" else 0.9,
        explanation="Según la EPA, el paro bajó un 14,6 %.",
        sources=[] if verdict == "no_verificable" else [INE],
    )


def promise(pid: str, speaker: str) -> Promise:
    return Promise(
        id=pid,
        speaker=speaker,
        kind="promesa",
        text_original="Subiremos el SMI a 1.500 euros",
        text_normalized="El SMI llegará a 1.500 euros en 2028.",
        claim_type="otro",
        assessment="dudosa",
        confidence=0.7,
        explanation="Depende de la negociación.",
        obstacles=["presupuesto"],
        sources=[INE],
    )


def report(*claims: Claim, promises: tuple[Promise, ...] = ()) -> Report:
    return Report(
        job_id=JOB,
        source="debate.txt",
        input_kind="texto",
        speech_date=dt.date(2026, 10, 18),
        generated_at=dt.datetime(2026, 10, 18, 22, 30, tzinfo=dt.UTC),
        speakers={"A": "Ana Ruiz (Partido A)", "B": "Luis Gil"},
        speaker_profiles=[
            Speaker(label="A", name="Ana Ruiz", party="Partido A", confidence=0.9),
            Speaker(label="B", name="Luis Gil", confidence=0.8),
        ],
        claims=list(claims),
        promises=list(promises),
        cost=RunCost(),
        warnings=["Un bloque falló"],
    )


# ── slug (sin base de datos) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Debate a tres", "2026-10-18-debate-a-tres"),
        ("¡Entrevista en «La 1»: Sánchez & Núñez!", "2026-10-18-entrevista-en-la-1-sanchez-nunez"),
        ("Sesión de control al Gobierno del miércoles en el Congreso de los Diputados",
         "2026-10-18-sesion-de-control-al-gobierno-del-miercoles-en-el"),
        ("¿?", "2026-10-18-informe"),
    ],
)  # fmt: skip
def test_slugs_are_readable_and_start_with_the_date(title, slug):
    assert slugify(dt.date(2026, 10, 18), title) == slug


# ── base de datos ────────────────────────────────────────────────────────────

pytest_supabase = pytest.mark.supabase


@pytest_supabase
def test_a_stored_report_comes_back_identical(db):  # noqa: F811
    original = report(claim("c1", "A"), claim("c2", "B", "falso"), promises=(promise("p1", "A"),))
    meta = create_report(
        db, job_id=JOB, titulo="Debate a tres", fecha=original.speech_date,
        input_kind="texto", fuente="debate.txt", origen="local", tipo="debate",
    )  # fmt: skip
    assert (meta.estado, meta.slug) == ("en_analisis", "2026-10-18-debate-a-tres")
    store_result(db, original)
    assert load_report(db, JOB).model_dump() == original.model_dump()
    meta = load_meta(db, JOB)
    assert meta.estado == "borrador" and meta.analizado_en is not None
    rows = db.execute(
        "select claim_id, kind, version, origen, etiqueta, categoria, orador, partido"
        " from app.claim_versions where report_id = %s order by orden",
        (JOB,),
    ).fetchall()
    assert rows == [
        ("c1", "afirmacion", 1, "analisis", "verdadero", "verdadero", "Ana Ruiz", "Partido A"),
        ("c2", "afirmacion", 1, "analisis", "falso", "falso_con_fuente_oficial", "Luis Gil", None),
        ("p1", "promesa", 1, "analisis", "dudosa", None, "Ana Ruiz", "Partido A"),
    ]


@pytest_supabase
def test_rerunning_an_analysis_keeps_the_earlier_versions(db):  # noqa: F811
    create_report(db, job_id=JOB, titulo="Debate", fecha=dt.date(2026, 10, 18),
                  input_kind="texto", fuente="debate.txt", origen="local")  # fmt: skip
    store_result(db, report(claim("c1", "A"), claim("c2", "B")))
    store_result(db, report(claim("c1", "A", "falso")))  # c1 changed, c2 gone
    again = load_report(db, JOB)
    assert [(c.id, c.verdict) for c in again.claims] == [("c1", "falso")]
    history = db.execute(
        "select claim_id, version, vigente, etiqueta from app.claim_versions"
        " where report_id = %s order by claim_id, version",
        (JOB,),
    ).fetchall()
    assert history == [
        ("c1", 1, False, "verdadero"),
        ("c1", 2, True, "falso"),
        ("c2", 1, False, "verdadero"),
    ]
    store_result(db, report(claim("c1", "A", "falso")))  # same result: no new version
    assert db.execute(
        "select count(*) from app.claim_versions where report_id = %s", (JOB,)
    ).fetchone() == (3,)


@pytest_supabase
def test_slugs_never_collide(db):  # noqa: F811
    common = dict(titulo="Debate", fecha=dt.date(2026, 10, 18), input_kind="texto",
                  fuente="x.txt", origen="local")  # fmt: skip
    assert create_report(db, job_id="aaaaaaaaaaaa", **common).slug == "2026-10-18-debate"
    assert create_report(db, job_id="bbbbbbbbbbbb", **common).slug == "2026-10-18-debate-2"


@pytest_supabase
def test_progress_and_errors_are_recorded(db):  # noqa: F811
    create_report(db, job_id=JOB, titulo="Debate", fecha=dt.date(2026, 10, 18),
                  input_kind="texto", fuente="x.txt", origen="servidor")  # fmt: skip
    set_progress(db, JOB, {"paso": "evidence", "hechos": ["claims", "context"]})
    assert load_meta(db, JOB).progreso["paso"] == "evidence"
    mark_error(db, JOB, "Límite de uso alcanzado")
    meta = load_meta(db, JOB)
    assert (meta.estado, meta.error) == ("error", "Límite de uso alcanzado")
    # A failed analysis can be run again: it goes back to analysis and then to draft.
    create_report(db, job_id=JOB, titulo="Debate", fecha=dt.date(2026, 10, 18),
                  input_kind="texto", fuente="x.txt", origen="servidor")  # fmt: skip
    assert load_meta(db, JOB).estado == "en_analisis"


@pytest_supabase
def test_a_published_report_is_never_overwritten_by_a_new_analysis(db):  # noqa: F811
    create_report(db, job_id=JOB, titulo="Debate", fecha=dt.date(2026, 10, 18),
                  input_kind="texto", fuente="x.txt", origen="local", tipo="debate")  # fmt: skip
    store_result(db, report(claim("c1", "A")))
    db.execute(
        "update app.reports set estado = 'publicado', publicado_en = now() where id = %s", (JOB,)
    )
    with pytest.raises(ReportError, match="publicado"):
        create_report(db, job_id=JOB, titulo="Debate", fecha=dt.date(2026, 10, 18),
                      input_kind="texto", fuente="x.txt", origen="local")  # fmt: skip
    with pytest.raises(ReportError, match="publicado"):
        store_result(db, report(claim("c1", "A", "falso")))


@pytest_supabase
def test_unknown_reports_are_an_error(db):  # noqa: F811
    with pytest.raises(ReportError, match="no existe"):
        load_report(db, "000000000000")
    with pytest.raises(ReportError, match="no existe"):
        store_result(db, report(claim("c1", "A")))
