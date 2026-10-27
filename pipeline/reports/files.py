"""Archivos de los informes en Supabase Storage (API REST, con la clave service_role).

Bucket privado `informes/<job_id>/…`: informe HTML e intermedios para auditar y re-evaluar
(transcripción, afirmaciones, evidencia, veredictos, manifest). Más adelante, un bucket
público para las imágenes de Instagram (la API de Meta las descarga de una URL pública).
La clave nunca aparece en un mensaje de error.
"""

import hashlib
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx
import psycopg
from pydantic import BaseModel

from pipeline.config import ConfigError, Settings
from pipeline.http import request_with_retries

PRIVATE_BUCKET = "informes"
TIMEOUT_SECONDS = 120
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
_SAFE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class StorageError(RuntimeError):
    """Storage refused or failed; the message never includes the key."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StoredFile(BaseModel):
    ruta: str  # bucket/path
    sha256: str
    bytes: int


def _check_path(path: str) -> str:
    if not _SAFE_PATH.match(path) or {".", ".."} & set(path.split("/")):
        raise StorageError(f"Ruta no válida en Storage: {path!r}")
    return path


def _check_bucket(bucket: str) -> str:
    if not _SAFE_BUCKET.match(bucket):
        raise StorageError(f"Nombre de bucket no válido: {bucket!r}")
    return bucket


def _check_url(url: str) -> str:
    """The key goes in every request: only over https (http only to this machine)."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return url
    raise ConfigError("SUPABASE_URL debe empezar por https:// (la clave no se envía sin cifrar)")


class Storage:
    def __init__(self, url: str, key: str, client: httpx.Client | None = None) -> None:
        self._base = _check_url(url).rstrip("/") + "/storage/v1"
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
        status = response.status_code
        if status >= 400:
            body = response.text.replace(self._key, "***")[:200]
            # Storage answers a missing object with 400 + "not_found" as well as with 404.
            if status == 404 or (status == 400 and '"not_found"' in body):
                raise StorageError(f"El archivo no existe en Storage (HTTP {status})", status)
            raise StorageError(f"Storage respondió HTTP {status}: {body}", status)
        return response

    def ensure_bucket(self, name: str, *, public: bool) -> None:
        _check_bucket(name)
        try:
            self._request(
                "POST", f"{self._base}/bucket", json={"id": name, "name": name, "public": public}
            )
        except StorageError as exc:
            duplicate = "Duplicate" in str(exc) or "already exists" in str(exc)
            if not (exc.status_code in (400, 409) and duplicate):
                raise

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> StoredFile:
        _check_bucket(bucket)
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
        _check_bucket(bucket)
        _check_path(path)
        return self._request("GET", f"{self._base}/object/authenticated/{bucket}/{path}").content

    def delete(self, bucket: str, paths: Iterable[str]) -> None:
        _check_bucket(bucket)
        prefixes = [_check_path(p) for p in paths]
        if prefixes:
            self._request("DELETE", f"{self._base}/object/{bucket}", json={"prefixes": prefixes})

    def public_url(self, bucket: str, path: str) -> str:
        return f"{self._base}/object/public/{_check_bucket(bucket)}/{_check_path(path)}"


def record_file(conn: psycopg.Connection, report_id: str, tipo: str, stored: StoredFile) -> None:
    """Register (or refresh) a report's file in app.report_files."""
    conn.execute(
        "insert into app.report_files (report_id, tipo, ruta, sha256, bytes)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (ruta) do update set sha256 = excluded.sha256, bytes = excluded.bytes,"
        " tipo = excluded.tipo, creado_en = now()",
        (report_id, tipo, stored.ruta, stored.sha256, stored.bytes),
    )
