"""Caché de las búsquedas de Tavily: en Postgres (ops.search_cache) y en disco
(data/cache/search/).

Cada búsqueda cuesta créditos y los análisis se repiten mucho (cambios de prompt,
evaluación, el mismo dato en varios discursos). La clave es el hash de la consulta
normalizada y de los parámetros; se guardan solo las respuestas correctas, con la fecha,
para poder auditarlas y caducarlas. Con base de datos se lee primero de ella (sobrevive a
un disco efímero y se comparte entre el Mac y el servidor); si falla, solo el disco.
"""

import datetime as dt
import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

import psycopg

from pipeline.evidence.passages import SPANISH_STOPWORDS

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("data") / "cache" / "search"


_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+", re.UNICODE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# Searches whose most recent year is at least this old are about a closed period: official
# figures for it are final (INE and Eurostat revise the last two years), so they never expire.
CLOSED_PERIOD_YEARS = 2


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _normalize_query(query: str) -> str:
    """Lowercase, no accents, no Spanish stopwords, decimal commas as dots, sorted tokens."""
    text = _strip_accents(query.casefold())
    tokens = {t.replace(",", ".") for t in _TOKEN_RE.findall(text)}
    return " ".join(sorted(t for t in tokens if t not in SPANISH_STOPWORDS))


def _hash(canonical: dict[str, Any]) -> str:
    if "include_domains" in canonical:
        canonical["include_domains"] = sorted(canonical["include_domains"])
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def cache_key(request: dict[str, Any]) -> str:
    """Hash of the normalized query and every parameter that changes the results."""
    return _hash({**request, "query": _normalize_query(str(request.get("query", "")))})


def legacy_cache_key(request: dict[str, Any]) -> str:
    """Key of the first version of the cache (only case and spaces normalized)."""
    query = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(request.get("query", ""))))
    return _hash({**request, "query": query.strip().casefold()})


Entry = tuple[dict[str, Any], dt.datetime]


class EntryStore(Protocol):
    def read(self, key: str) -> Entry | None: ...

    def write(
        self, key: str, request: dict[str, Any], response: dict[str, Any], saved_at: dt.datetime
    ) -> None: ...


DB_UNAVAILABLE = (
    "La caché de búsquedas en Supabase no estuvo disponible: solo se usó la del disco"
    " (puede gastar créditos de más)"
)


class DbSearchStore:
    """ops.search_cache. After the first database error it is left aside for the rest of
    the run (one warning, no slow retries on every search): the disk cache keeps working,
    and `unavailable` lets the run say so in its warnings. With `strict` (the one-off
    migration) any error is raised instead: nothing may be counted as copied."""

    def __init__(
        self,
        connect: Callable[[], AbstractContextManager[psycopg.Connection]],
        *,
        strict: bool = False,
    ) -> None:
        self._connect = connect
        self._strict = strict
        self._broken = False

    @property
    def unavailable(self) -> bool:
        return self._broken

    def _failed(self, exc: Exception) -> None:
        if self._strict:
            raise exc
        self._broken = True
        logger.warning("Caché de búsquedas en la base no disponible; solo disco: %s", exc)

    def read(self, key: str) -> Entry | None:
        if self._broken:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "select respuesta, guardado_en from ops.search_cache where clave = %s", (key,)
                ).fetchone()
        except (psycopg.Error, OSError) as exc:
            self._failed(exc)
            return None
        if row is not None and not isinstance(row[0], dict):
            logger.warning("Entrada de caché %s en la base no válida; se busca de nuevo", key)
            return None
        return (row[0], row[1]) if row else None

    def write(
        self, key: str, request: dict[str, Any], response: dict[str, Any], saved_at: dt.datetime
    ) -> None:
        if self._broken:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "insert into ops.search_cache (clave, consulta, respuesta, guardado_en)"
                    " values (%s, %s, %s, %s) on conflict (clave) do update set"
                    " consulta = excluded.consulta, respuesta = excluded.respuesta,"
                    " guardado_en = excluded.guardado_en",
                    (
                        key,
                        json.dumps(request, ensure_ascii=False),
                        json.dumps(response, ensure_ascii=False),
                        saved_at,
                    ),
                )
        except (psycopg.Error, OSError) as exc:
            self._failed(exc)


class SearchCache:
    def __init__(
        self,
        root: Path = DEFAULT_DIR,
        *,
        max_age_days: int = 30,
        read: bool = True,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        db: EntryStore | None = None,
    ) -> None:
        self.root = root
        self.max_age = dt.timedelta(days=max_age_days)
        # False (--no-cache): always search, but still store the fresh result.
        self.read = read
        self._now = now
        self._db = db

    @property
    def db_unavailable(self) -> bool:
        return bool(getattr(self._db, "unavailable", False))

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """The stored response, or None if missing, expired, unreadable or not reading.
        Entries saved with the old key are found too, and copied under the new one."""
        if not self.read:
            return None
        key = cache_key(request)
        if self._db is not None:
            row = self._db.read(key)
            if row is not None and isinstance(row[0], dict):
                return None if self._expired(request, row[1]) else row[0]
        found = self._read(self._path(key), request)
        if found is not None and self._db is not None:
            self._db.write(key, request, found[0], found[1])  # keeps its original age
        if found is None:
            found = self._read(self._path(legacy_cache_key(request)), request)
            if found is not None:
                self.put(request, found[0], saved_at=found[1])  # keeps its original age
        return found[0] if found else None

    def _expired(self, request: dict[str, Any], saved_at: dt.datetime) -> bool:
        years = [int(y) for y in _YEAR_RE.findall(str(request.get("query", "")))]
        if years and max(years) <= self._now().year - CLOSED_PERIOD_YEARS:
            return False
        return self._now() - saved_at > self.max_age

    def _read(
        self, path: Path, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dt.datetime] | None:
        try:
            data = json.loads(path.read_text())
            saved_at = dt.datetime.fromisoformat(data["saved_at"])
            response = data["response"]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Entrada de caché ilegible %s; se busca de nuevo: %s", path, exc)
            return None
        if self._expired(request, saved_at):
            return None
        return (response, saved_at) if isinstance(response, dict) else None

    def put(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        saved_at: dt.datetime | None = None,
    ) -> None:
        """Store atomically; a failed write only loses the cache entry."""
        key = cache_key(request)
        if self._db is not None:
            self._db.write(key, request, response, saved_at or self._now())
        path = self._path(key)
        when = (saved_at or self._now()).isoformat()
        entry = {"saved_at": when, "request": request, "response": response}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(entry, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("No se pudo guardar la búsqueda en caché (%s): %s", path, exc)
