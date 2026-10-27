"""Programas electorales: semilla, extracción del texto, fragmentos y candidatos de archive.org."""

import datetime as dt
from pathlib import Path

import pytest

from pipeline.knowledge.programs import (
    SEED_PATH,
    ProgramText,
    archive_raw_url,
    chunk_pages,
    extract_pages,
    load_seed,
    wayback_candidates,
)


def _pdf(pages: list[str]) -> bytes:
    """Minimal valid PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font = 3 + 2 * n
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R"
            f" /Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


# ── semilla ──────────────────────────────────────────────────────────────────


def test_the_versioned_seed_is_valid_and_follows_the_rule():
    seed = load_seed()
    assert [e.fecha for e in seed.elecciones] == [
        dt.date(2016, 6, 26),
        dt.date(2019, 4, 28),
        dt.date(2019, 11, 10),
        dt.date(2023, 7, 23),
    ]
    ids = [p.id for p in seed.programas]
    assert len(ids) == len(set(ids))
    pairs = [(p.eleccion, p.partido) for p in seed.programas]
    assert len(pairs) == len(set(pairs))  # one programme per party and election
    assert all(p.grupo not in ("Mixto", "Plural") for p in seed.programas)


def _seed(tmp_path: Path, programa: str) -> Path:
    path = tmp_path / "programas.yaml"
    path.write_text(
        "elecciones: [{fecha: 2023-07-23, legislatura: XV}]\n"
        "partidos: {aa: {nombre: Partido A, dominios: [partido-a.es]}}\n"
        f"programas: [{programa}]\n"
    )
    return path


BASE = "id: aa-2023-07, eleccion: 2023-07-23, partido: aa, candidatura: A, grupo: A, idioma: es"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.partido-a.es/programa.pdf",
        "https://partido-a.es/docs/programa.pdf",
        "https://web.archive.org/web/20230710000000id_/https://partido-a.es/p.pdf",
    ],
)
def test_urls_on_the_party_domain_or_its_archive_copy_are_accepted(tmp_path, url):
    seed = load_seed(_seed(tmp_path, f"{{{BASE}, url: '{url}'}}"))
    assert seed.programas[0].url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://loentiendo.com/programa-a.pdf",  # aggregator
        "https://partido-a.es.example.com/p.pdf",  # look-alike host
        "https://web.archive.org/web/2023/https://otro.es/p.pdf",  # archive of another site
    ],
)
def test_copies_outside_the_party_domain_are_rejected(tmp_path, url):
    with pytest.raises(ValueError, match="dominio oficial"):
        load_seed(_seed(tmp_path, f"{{{BASE}, url: '{url}'}}"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("eleccion: 2020-01-01", "no está en la lista de elecciones"),
        ("partido: zz", "partido desconocido"),
        ("grupo: Mixto", "Mixto"),
    ],
)
def test_inconsistent_entries_are_rejected(tmp_path, change, message):
    key = change.split(":")[0]
    fields = ", ".join(f for f in BASE.split(", ") if not f.startswith(key + ":"))
    with pytest.raises(ValueError, match=message):
        load_seed(_seed(tmp_path, f"{{{fields}, {change}}}"))


# ── texto ────────────────────────────────────────────────────────────────────


def test_text_is_extracted_page_by_page():
    pages = extract_pages(_pdf(["Primera pagina del programa", "", "Tercera: vivienda"]))
    assert len(pages) == 3
    assert "Primera pagina" in pages[0] and pages[1].strip() == "" and "vivienda" in pages[2]


def test_a_file_that_is_not_a_pdf_is_rejected():
    with pytest.raises(ValueError, match="PDF"):
        extract_pages(b"<html>no es un pdf</html>")


def test_chunks_keep_their_page_and_never_cross_pages():
    pages = ["Uno.\n\nDos   con   espacios.", "   \n  ", "Tres."]
    chunks = chunk_pages(pages, max_chars=1000)
    assert [(c.pagina, c.texto) for c in chunks] == [(1, "Uno.\n\nDos con espacios."), (3, "Tres.")]
    assert [c.ordinal for c in chunks] == [0, 1]


def test_long_pages_are_split_by_paragraph_and_long_paragraphs_by_size():
    para = "palabra " * 50  # ~400 characters
    chunks = chunk_pages([f"{para}\n\n{para}\n\n{'x' * 900}"], max_chars=500)
    assert all(len(c.texto) <= 500 for c in chunks)
    assert {c.pagina for c in chunks} == {1}
    assert len(chunks) >= 3
    assert "".join(c.texto for c in chunks).count("palabra") == 100


def test_program_text_round_trips_through_json(tmp_path):
    text = ProgramText(
        id="aa-2023-07",
        url="https://partido-a.es/p.pdf",
        sha256="a" * 64,
        paginas=2,
        chunks=chunk_pages(["Uno", "Dos"]),
    )
    path = tmp_path / "aa.json"
    path.write_text(text.model_dump_json())
    assert ProgramText.model_validate_json(path.read_text()) == text


# ── candidatos en archive.org ────────────────────────────────────────────────


def test_archive_candidates_are_pdfs_of_the_party_domain_before_the_vote():
    rows = [
        ["timestamp", "original", "mimetype", "statuscode", "digest"],
        ["20230705101010", "https://partido-a.es/programa-electoral-2023.pdf", "application/pdf",
         "200", "D1"],
        ["20230706101010", "https://partido-a.es/programa-electoral-2023.pdf", "application/pdf",
         "200", "D1"],  # same document captured twice
        ["20230707101010", "https://partido-a.es/otro.pdf", "application/pdf", "200", "D2"],
        ["20230708101010", "https://partido-a.es/programa.html", "text/html", "200", "D3"],
        ["20230709101010", "https://partido-a.es/Programa_Marco.pdf", "application/pdf", "404",
         "D4"],
    ]  # fmt: skip
    found = wayback_candidates(rows)
    assert [c.original for c in found] == [
        "https://partido-a.es/programa-electoral-2023.pdf",
        "https://partido-a.es/otro.pdf",
    ]
    assert found[0].keyword  # "programa" in the name: shown first
    assert not found[1].keyword
    assert found[0].archive_url == (
        "https://web.archive.org/web/20230705101010id_/https://partido-a.es/programa-electoral-2023.pdf"
    )


def test_archive_raw_url_asks_for_the_original_bytes():
    assert archive_raw_url("20230705101010", "https://a.es/p.pdf") == (
        "https://web.archive.org/web/20230705101010id_/https://a.es/p.pdf"
    )


def test_seed_path_points_to_the_versioned_file():
    assert SEED_PATH.name == "programas.yaml" and SEED_PATH.exists()


# ── comando buscar-programa (sin base de datos) ──────────────────────────────


def test_search_command_prints_page_quote_and_url(monkeypatch):
    from typer.testing import CliRunner

    from pipeline.knowledge import cli, programs
    from pipeline.knowledge.programs import ProgramPassage

    from .test_knowledge_review_cli import FakeConn

    calls = []

    def fake_search(conn, query, **kwargs):
        calls.append((query, kwargs))
        return [
            ProgramPassage(
                program_id="aa-2023-07", partido="aa", candidatura="Candidatura A",
                eleccion=dt.date(2023, 7, 23), idioma="ca", pagina=12,
                url="https://partido-a.es/p.pdf", fragmento="… «vivienda» pública …", rank=0.5,
            )
        ]  # fmt: skip

    monkeypatch.setattr(cli, "_connect", FakeConn)
    monkeypatch.setattr(programs, "search_programs", fake_search)
    result = CliRunner().invoke(
        cli.app, ["buscar-programa", "vivienda", "--partido", "aa", "--eleccion", "2023-07-23"]
    )
    assert result.exit_code == 0, result.output
    assert "Candidatura A · 2023-07-23 · pág. 12" in result.output
    assert "«vivienda»" in result.output and "https://partido-a.es/p.pdf" in result.output
    assert "diccionario español" in result.output  # a Catalan programme: recall warning
    assert calls == [
        (
            "vivienda",
            {"partido": "aa", "eleccion": dt.date(2023, 7, 23), "include_pending": False,
             "limit": 10},
        )
    ]  # fmt: skip


def test_search_command_rejects_a_bad_date_before_connecting(monkeypatch):
    from typer.testing import CliRunner

    from pipeline.knowledge import cli

    monkeypatch.setattr(cli, "_connect", lambda: pytest.fail("no debe conectar"))
    result = CliRunner().invoke(cli.app, ["buscar-programa", "x", "--eleccion", "23-07-2023"])
    assert result.exit_code == 2 and "Fecha no válida" in result.output


def test_a_broken_or_hostile_pdf_is_a_value_error():
    with pytest.raises(ValueError, match="No se pudo leer el PDF"):
        extract_pages(b"%PDF-1.7\n" + b"\x00basura" * 50)


def test_an_encrypted_pdf_is_rejected():
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("clave")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ValueError, match="cifrado"):
        extract_pages(buffer.getvalue())


def test_live_party_urls_must_be_https():
    from pipeline.knowledge.programs import official_url

    assert official_url("https://www.partido.es/programa.pdf", ["partido.es"])
    assert not official_url("http://www.partido.es/programa.pdf", ["partido.es"])
    assert official_url(
        "https://web.archive.org/web/2016id_/http://www.partido.es/p.pdf", ["partido.es"]
    )
    assert not official_url(
        "http://web.archive.org/web/2016id_/http://www.partido.es/p.pdf", ["partido.es"]
    )


def test_malformed_archive_answers_are_handled():
    header = ["timestamp", "original", "mimetype", "statuscode", "digest"]
    good = ["20160601", "https://partido.es/programa.pdf", "application/pdf", "200", "A"]
    assert len(wayback_candidates([header, ["corta"], good])) == 1
    with pytest.raises(ValueError, match="columnas"):
        wayback_candidates([["timestamp", "original"], good])
