"""Cliente de Supabase Storage (API simulada con respx: nunca se llama a Supabase de verdad)."""

import hashlib
import json

import httpx
import pytest
import respx

from pipeline.config import ConfigError, Settings
from pipeline.reports.files import Storage, StorageError

URL = "https://proyecto.supabase.co"
KEY = "clave-service-role-de-prueba"


@pytest.fixture
def storage():
    with httpx.Client() as client:
        yield Storage(URL, KEY, client=client)


@respx.mock
def test_upload_sends_the_key_and_returns_size_and_hash(storage):
    route = respx.post(f"{URL}/storage/v1/object/informes/abc/informe.html").mock(
        return_value=httpx.Response(200, json={"Key": "informes/abc/informe.html"})
    )
    stored = storage.upload("informes", "abc/informe.html", b"<html></html>", "text/html")
    assert stored.ruta == "informes/abc/informe.html"
    assert stored.bytes == 13 and stored.sha256 == hashlib.sha256(b"<html></html>").hexdigest()
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["apikey"] == KEY
    assert request.headers["x-upsert"] == "true"
    assert request.headers["content-type"] == "text/html"


@respx.mock
def test_errors_never_show_the_key(storage):
    respx.post(f"{URL}/storage/v1/object/informes/abc/x.json").mock(
        return_value=httpx.Response(403, json={"message": f"bad key {KEY}"})
    )
    with pytest.raises(StorageError) as info:
        storage.upload("informes", "abc/x.json", b"{}", "application/json")
    assert KEY not in str(info.value) and "403" in str(info.value)


@respx.mock
def test_download_and_delete(storage):
    respx.get(f"{URL}/storage/v1/object/authenticated/informes/abc/x.json").mock(
        return_value=httpx.Response(200, content=b'{"a": 1}')
    )
    assert storage.download("informes", "abc/x.json") == b'{"a": 1}'
    route = respx.delete(f"{URL}/storage/v1/object/informes").mock(
        return_value=httpx.Response(200, json=[])
    )
    storage.delete("informes", ["abc/x.json", "abc/y.json"])
    assert json.loads(route.calls.last.request.content) == {
        "prefixes": ["abc/x.json", "abc/y.json"]
    }


@respx.mock
def test_missing_files_are_reported_as_such(storage):
    respx.get(f"{URL}/storage/v1/object/authenticated/informes/abc/nada.json").mock(
        return_value=httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
    )
    with pytest.raises(StorageError, match="no existe"):
        storage.download("informes", "abc/nada.json")


@respx.mock
def test_buckets_are_created_once(storage):
    route = respx.post(f"{URL}/storage/v1/bucket").mock(
        side_effect=[
            httpx.Response(200, json={"name": "informes"}),
            httpx.Response(400, json={"statusCode": "409", "error": "Duplicate"}),
        ]
    )
    storage.ensure_bucket("informes", public=False)
    storage.ensure_bucket("informes", public=False)  # already there: not an error
    assert json.loads(route.calls[0].request.content) == {
        "id": "informes",
        "name": "informes",
        "public": False,
    }


@pytest.mark.parametrize("path", ["../otro/x", "abc//x", "/abc", "abc/x y", ""])
def test_unsafe_paths_are_rejected(storage, path):
    with pytest.raises(StorageError, match="Ruta"):
        storage.upload("informes", path, b"", "text/plain")


def test_public_url_points_to_the_public_object():
    storage = Storage(URL + "/", KEY)
    assert storage.public_url("publico", "ig/1.jpg") == (
        f"{URL}/storage/v1/object/public/publico/ig/1.jpg"
    )


def test_settings_without_supabase_storage_are_a_config_error():
    with pytest.raises(ConfigError, match="SUPABASE_URL"):
        Storage.from_settings(Settings(supabase_service_role_key=KEY))
    with pytest.raises(ConfigError, match="SUPABASE_SERVICE_ROLE_KEY"):
        Storage.from_settings(Settings(supabase_url=URL))


@pytest.mark.parametrize("url", ["http://proyecto.supabase.co", "ftp://x", "proyecto.supabase.co"])
def test_the_key_is_only_sent_over_https(url):
    with pytest.raises(ConfigError, match="https"):
        Storage(url, KEY)


def test_plain_http_is_accepted_only_for_this_machine():
    Storage("http://127.0.0.1:54321", KEY)
    Storage("http://localhost:54321", KEY)


@pytest.mark.parametrize("bucket", ["../x", "Informes", "a", "informes/otro", ""])
def test_bucket_names_are_validated(storage, bucket):
    with pytest.raises(StorageError, match="bucket"):
        storage.upload(bucket, "abc/x.json", b"{}", "application/json")
    with pytest.raises(StorageError, match="bucket"):
        storage.public_url(bucket, "abc/x.json")


@pytest.mark.parametrize("path", ["abc/../x.json", "./x.json", "abc/./x.json", "/abc", "a b"])
def test_paths_are_validated(storage, path):
    with pytest.raises(StorageError, match="Ruta"):
        storage.upload("informes", path, b"{}", "application/json")


@respx.mock
def test_a_missing_object_is_told_by_its_status_not_by_its_text(storage):
    respx.get(f"{URL}/storage/v1/object/authenticated/informes/abc/x.json").mock(
        return_value=httpx.Response(404, json={"error": "not_found"})
    )
    with pytest.raises(StorageError, match="no existe") as info:
        storage.download("informes", "abc/x.json")
    assert info.value.status_code == 404
    respx.post(f"{URL}/storage/v1/object/informes/abc/404.json").mock(
        return_value=httpx.Response(413, json={"message": "Payload too large: 4040 bytes"})
    )
    with pytest.raises(StorageError, match="HTTP 413") as info:
        storage.upload("informes", "abc/404.json", b"{}", "application/json")
    assert "no existe" not in str(info.value)


@respx.mock
def test_an_existing_bucket_is_fine_but_other_errors_are_not(storage):
    route = respx.post(f"{URL}/storage/v1/bucket")
    route.mock(return_value=httpx.Response(409, json={"error": "Duplicate", "message": "x"}))
    storage.ensure_bucket("informes", public=False)
    route.mock(return_value=httpx.Response(403, json={"message": "Duplicate? no: forbidden"}))
    with pytest.raises(StorageError, match="403"):
        storage.ensure_bucket("informes", public=False)
