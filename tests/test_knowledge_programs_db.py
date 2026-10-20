"""Programas electorales en Supabase: carga pendiente, revisión, búsqueda e inmutabilidad."""

import datetime as dt

import psycopg
import pytest

from pipeline.knowledge.programs import (
    ProgramsSeed,
    ProgramText,
    chunk_pages,
    load_programs,
    search_programs,
)
from pipeline.knowledge.review import decide, pending_items

from .test_knowledge_schema import db  # noqa: F401  (fixture)

pytestmark = pytest.mark.supabase

URL = "https://web.archive.org/web/20230705101010id_/https://partido-a.es/programa.pdf"


def _seed(url: str | None = URL) -> ProgramsSeed:
    return ProgramsSeed.model_validate(
        {
            "elecciones": [{"fecha": "2023-07-23", "legislatura": "XV"}],
            "partidos": {"aa": {"nombre": "Partido A", "dominios": ["partido-a.es"]}},
            "programas": [
                {
                    "id": "aa-2023-07",
                    "eleccion": "2023-07-23",
                    "partido": "aa",
                    "candidatura": "Candidatura A",
                    "grupo": "Grupo A",
                    "idioma": "es",
                    "url": url,
                }
            ],
        }
    )


def _collected(tmp_path, sha="a" * 64, url=URL):
    pages = [
        "Índice",
        "Vivienda. Construiremos viviendas de alquiler asequible en suelo público.",
        "Fiscalidad. Rebajaremos el IRPF a las rentas medias.",
    ]
    text = ProgramText(id="aa-2023-07", url=url, sha256=sha, paginas=3, chunks=chunk_pages(pages))
    (tmp_path / "aa-2023-07.json").write_text(text.model_dump_json())
    return tmp_path


def test_programmes_enter_pending_and_are_not_searchable_until_approved(db, tmp_path):  # noqa: F811
    report = load_programs(db, _seed(), _collected(tmp_path))
    assert report.new == ["aa-2023-07"]
    row = db.execute(
        "select estado, url_original, paginas from ref.programs where id = 'aa-2023-07'"
    ).fetchone()
    assert row == ("pendiente", "https://partido-a.es/programa.pdf", 3)
    assert search_programs(db, "viviendas alquiler") == []
    pending = search_programs(db, "viviendas alquiler", include_pending=True)
    assert [(r.program_id, r.pagina) for r in pending] == [("aa-2023-07", 2)]

    item = next(i for i in pending_items(db, ["programa"]) if i.id == "aa-2023-07")
    card = "\n".join(item.lines)
    assert "Grupo A" in card and "SHA-256" in card and "partido-a.es/programa.pdf" in card
    decide(db, "programa", "aa-2023-07", "aprobado", reviewer="Jorge")

    found = search_programs(db, "IRPF rentas", partido="aa", eleccion=dt.date(2023, 7, 23))
    assert [(r.program_id, r.pagina) for r in found] == [("aa-2023-07", 3)]
    assert "«" in found[0].fragmento  # the matching words are marked
    assert search_programs(db, "IRPF", partido="bb") == []


def test_reloading_the_same_pdf_changes_nothing_and_another_pdf_is_flagged(db, tmp_path):  # noqa: F811
    load_programs(db, _seed(), _collected(tmp_path))
    assert load_programs(db, _seed(), tmp_path).unchanged == ["aa-2023-07"]
    other = tmp_path / "otro"
    other.mkdir()
    assert load_programs(db, _seed(), _collected(other, sha="b" * 64)).changed == ["aa-2023-07"]


def test_entries_without_url_or_text_are_reported_not_loaded(db, tmp_path):  # noqa: F811
    assert load_programs(db, _seed(url=None), tmp_path).without_url == ["aa-2023-07"]
    assert load_programs(db, _seed(), tmp_path).not_collected == ["aa-2023-07"]


def test_the_text_and_identity_of_a_programme_cannot_be_changed(db, tmp_path):  # noqa: F811
    load_programs(db, _seed(), _collected(tmp_path))
    for sql in (
        "update ref.program_chunks set texto = 'otro' where program_id = 'aa-2023-07'",
        "delete from ref.program_chunks where program_id = 'aa-2023-07'",
        "update ref.programs set sha256 = repeat('c', 64) where id = 'aa-2023-07'",
        "delete from ref.programs where id = 'aa-2023-07'",
    ):
        db.execute("savepoint s")
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(sql)
        db.execute("rollback to savepoint s")
