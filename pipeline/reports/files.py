"""Archivos de los informes en Supabase Storage (API REST, con la clave service_role).

Bucket privado `informes/<job_id>/…`: informe HTML e intermedios para auditar y re-evaluar
(transcripción, afirmaciones, evidencia, veredictos, manifest). Más adelante, un bucket
público para las imágenes de Instagram (la API de Meta las descarga de una URL pública).
La clave nunca aparece en un mensaje de error.
"""

import hashlib
import re
from collections.abc import Iterable

import httpx
import psycopg
from pydantic import BaseModel

from pipeline.config import ConfigError, Settings
from pipeline.http import request_with_retries

PRIVATE_BUCKET = "informes"
TIMEOUT_SECONDS = 120
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")


class StorageError(RuntimeError):
    """Storage refused or failed; the message never includes the key."""


class StoredFile(BaseModel):
    ruta: str  # bucket/path
    sha256: str
    bytes: int


def _check_path(path: str) -> str:
    if not _SAFE_PATH.match(path) or ".." in path.split("/"):
        raise StorageError(f"Ruta no válida en Storage: {path!r}")
    return path


class Storage:
    def __init__(self, url: str, key: str, client: httpx.Client | None = None) -> None:
        self._base = url.rstrip("/") + "/storage/v1"
        self._key = key
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Storage":
        if not settings.supabase_url:
            raise ConfigError("Falta SUPABASE_URL en .env (Supabase → Project Settings → API)")
        if settings.supabase_service_role_key is None:
            raise ConfigError("Falta SUPABASE_SERVICE_ROLE_KEY en .env")
        return cls(settings.supabase_url, settings.supabase_service_role_key.get_secret_value())

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._key}", "apikey": self._key}
        headers.update(kwargs.pop("headers", {}))
        response = request_with_retries(
            self._client, method, url, max_attempts=3, backoff=1.0, error_cls=StorageError,
            headers=headers, **kwargs,
        )  # fmt: skip
        if response.status_code >= 400:
            body = response.text.replace(self._key, "***")[:200]
            if "not_found" in body or "404" in body:
                raise StorageError(f"El archivo no existe en Storage (HTTP {response.status_code})")
            raise StorageError(f"Storage respondió HTTP {response.status_code}: {body}")
        return response

    def ensure_bucket(self, name: str, *, public: bool) -> None:
        try:
            self._request(
                "POST", f"{self._base}/bucket", json={"id": name, "name": name, "public": public}
            )
        except StorageError as exc:
            if "Duplicate" not in str(exc) and "already exists" not in str(exc):
                raise

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> StoredFile:
        _check_path(path)
        self._request(
            "POST",
            f"{self._base}/object/{bucket}/{path}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        return StoredFile(
            ruta=f"{bucket}/{path}", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)
        )

    def download(self, bucket: str, path: str) -> bytes:
        _check_path(path)
        return self._request("GET", f"{self._base}/object/authenticated/{bucket}/{path}").content

    def delete(self, bucket: str, paths: Iterable[str]) -> None:
        prefixes = [_check_path(p) for p in paths]
        if prefixes:
            self._request("DELETE", f"{self._base}/object/{bucket}", json={"prefixes": prefixes})

    def public_url(self, bucket: str, path: str) -> str:
        return f"{self._base}/object/public/{bucket}/{_check_path(path)}"


def record_file(conn: psycopg.Connection, report_id: str, tipo: str, stored: StoredFile) -> None:
    """Register (or refresh) a report's file in app.report_files."""
    conn.execute(
        "insert into app.report_files (report_id, tipo, ruta, sha256, bytes)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (ruta) do update set sha256 = excluded.sha256, bytes = excluded.bytes,"
        " tipo = excluded.tipo, creado_en = now()",
        (report_id, tipo, stored.ruta, stored.sha256, stored.bytes),
    )
