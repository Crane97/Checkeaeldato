"""Descarga de programas: solo del dominio oficial o de archive.org, con tamaño máximo."""

import importlib.util
from pathlib import Path

import httpx
import pytest
import respx

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recopilar_programas.py"
spec = importlib.util.spec_from_file_location("recopilar_programas", SCRIPT)
recopilar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recopilar)

DOMAINS = ["partido.es"]
PDF = b"%PDF-1.7\ncontenido"


@respx.mock
def test_redirects_within_the_party_domain_and_archive_are_followed():
    respx.get("https://partido.es/p.pdf").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.partido.es/v2/p.pdf"})
    )
    respx.get("https://www.partido.es/v2/p.pdf").mock(return_value=httpx.Response(200, content=PDF))
    with httpx.Client() as client:
        assert recopilar._download(client, "https://partido.es/p.pdf", DOMAINS) == PDF


@respx.mock
@pytest.mark.parametrize(
    "target",
    ["https://otro.com/p.pdf", "http://www.partido.es/p.pdf", "https://169.254.169.254/latest"],
)
def test_a_redirect_out_of_the_allowed_hosts_is_refused(target):
    respx.get("https://partido.es/p.pdf").mock(
        return_value=httpx.Response(301, headers={"location": target})
    )
    with httpx.Client() as client, pytest.raises(ValueError, match="fuera del dominio"):
        recopilar._download(client, "https://partido.es/p.pdf", DOMAINS)


@respx.mock
def test_endless_redirects_stop():
    respx.get("https://partido.es/p.pdf").mock(
        return_value=httpx.Response(302, headers={"location": "https://partido.es/p.pdf"})
    )
    with httpx.Client() as client, pytest.raises(ValueError, match="redirecciones"):
        recopilar._download(client, "https://partido.es/p.pdf", DOMAINS)


@respx.mock
def test_oversized_files_and_non_pdfs_are_cut(monkeypatch):
    monkeypatch.setattr(recopilar, "MAX_PDF_BYTES", 100)
    respx.get("https://partido.es/grande.pdf").mock(
        return_value=httpx.Response(200, content=PDF + b"x" * 200)
    )
    respx.get("https://partido.es/pagina.pdf").mock(
        return_value=httpx.Response(200, content=b"<html>no</html>")
    )
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="bytes"):
            recopilar._download(client, "https://partido.es/grande.pdf", DOMAINS)
        with pytest.raises(ValueError, match="no es un PDF"):
            recopilar._download(client, "https://partido.es/pagina.pdf", DOMAINS)


def test_a_truncated_cache_file_counts_as_not_collected(tmp_path):
    out = tmp_path / "pp-2016-06.json"
    out.write_text('{"id": "pp-2016-06", "url": "https://')
    assert recopilar._already_collected(out, "https://partido.es/p.pdf") is False
    assert recopilar._already_collected(tmp_path / "no.json", "x") is False


@pytest.mark.parametrize(
    "url",
    [
        "https://web.archive.org/web/2016id_/https://otro.com/p.pdf",  # archive of another site
        "https://web.archive.org.evil.com/web/2016id_/https://partido.es/p.pdf",
        "https://web.archive.org@evil.com/p.pdf",
        "https://partido.es:8443/p.pdf",
        "https://user@partido.es/p.pdf",
    ],
)
def test_lookalike_hosts_ports_and_credentials_are_refused(url):
    assert recopilar._allowed(url, DOMAINS) is False


def test_the_archive_copy_of_the_party_domain_is_allowed():
    assert recopilar._allowed(
        "https://web.archive.org/web/20160601id_/http://www.partido.es/p.pdf", DOMAINS
    )
