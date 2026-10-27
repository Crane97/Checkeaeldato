"""Caché de búsquedas en Postgres (ops.search_cache): sobrevive a un disco efímero."""

import datetime as dt
from contextlib import nullcontext

import psycopg
import pytest

from pipeline.evidence.search_cache import DbSearchStore, SearchCache, cache_key

from .test_knowledge_schema import db  # noqa: F401  (fixture)

NOW = dt.datetime(2026, 10, 20, 12, tzinfo=dt.UTC)
REQUEST = {"query": "paro EPA 2026", "search_depth": "basic", "max_results": 5}
RESPONSE = {"results": [{"url": "https://www.ine.es/epa", "content": "10,6 %"}]}


class MemoryStore:
    """Stands in for DbSearchStore: same interface, kept in a dict."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[dict, dt.datetime]] = {}

    def read(self, key: str):
        return self.rows.get(key)

    def write(self, key, request, response, saved_at) -> None:
        self.rows[key] = (response, saved_at)


def _cache(tmp_path, store, **kwargs) -> SearchCache:
    return SearchCache(tmp_path / "search", db=store, now=lambda: NOW, **kwargs)


def test_a_search_saved_in_the_database_survives_losing_the_disk(tmp_path):
    store = MemoryStore()
    _cache(tmp_path / "a", store).put(REQUEST, RESPONSE)
    # Another machine, or the same one after a deploy: empty disk.
    assert _cache(tmp_path / "b", store).get(REQUEST) == RESPONSE


def test_entries_only_on_disk_are_copied_to_the_database_keeping_their_age(tmp_path):
    SearchCache(tmp_path / "search", now=lambda: NOW - dt.timedelta(days=10)).put(REQUEST, RESPONSE)
    store = MemoryStore()
    assert _cache(tmp_path, store).get(REQUEST) == RESPONSE
    assert store.rows[cache_key(REQUEST)] == (RESPONSE, NOW - dt.timedelta(days=10))


def test_expiry_applies_to_database_entries_too(tmp_path):
    store = MemoryStore()
    store.rows[cache_key(REQUEST)] = (RESPONSE, NOW - dt.timedelta(days=31))
    assert _cache(tmp_path, store, max_age_days=30).get(REQUEST) is None


def test_no_cache_still_writes_to_the_database(tmp_path):
    store = MemoryStore()
    cache = _cache(tmp_path, store, read=False)
    assert cache.get(REQUEST) is None
    cache.put(REQUEST, RESPONSE)
    assert cache_key(REQUEST) in store.rows


def test_a_database_failure_falls_back_to_the_disk_and_warns_once(tmp_path, caplog):
    calls = []

    def broken():
        calls.append(1)
        raise psycopg.OperationalError("sin conexión")

    store = DbSearchStore(broken)
    cache = _cache(tmp_path, store)
    cache.put(REQUEST, RESPONSE)
    assert cache.get(REQUEST) == RESPONSE  # from disk
    assert len(calls) == 1  # not retried on every search once it failed
    assert sum("caché de búsquedas" in r.message.lower() for r in caplog.records) == 1


@pytest.mark.supabase
def test_the_database_store_round_trips(db):  # noqa: F811
    store = DbSearchStore(lambda: nullcontext(db))
    assert store.read(cache_key(REQUEST)) is None
    store.write(cache_key(REQUEST), REQUEST, RESPONSE, NOW)
    assert store.read(cache_key(REQUEST)) == (RESPONSE, NOW)
    newer = {"results": []}
    store.write(cache_key(REQUEST), REQUEST, newer, NOW + dt.timedelta(days=1))
    assert store.read(cache_key(REQUEST)) == (newer, NOW + dt.timedelta(days=1))


def test_the_run_can_tell_the_database_cache_failed(tmp_path):
    def broken():
        raise psycopg.OperationalError("sin conexión")

    store = DbSearchStore(broken)
    cache = _cache(tmp_path, store)
    assert cache.db_unavailable is False
    cache.get(REQUEST)
    assert store.unavailable and cache.db_unavailable


def test_a_strict_store_raises_instead_of_falling_back():
    def broken():
        raise psycopg.OperationalError("sin conexión")

    store = DbSearchStore(broken, strict=True)
    with pytest.raises(psycopg.OperationalError):
        store.read(cache_key(REQUEST))
    with pytest.raises(psycopg.OperationalError):
        store.write(cache_key(REQUEST), REQUEST, RESPONSE, NOW)


@pytest.mark.supabase
def test_nul_characters_from_a_page_do_not_break_the_database_cache(db):  # noqa: F811
    store = DbSearchStore(lambda: nullcontext(db), strict=True)
    dirty = {"results": [{"content": "se contabiliza\x00 para cada especie", "x": ["a\x00b"]}]}
    store.write(cache_key(REQUEST), {**REQUEST, "query": "paro\x00"}, dirty, NOW)
    response, _ = store.read(cache_key(REQUEST))
    assert response == {"results": [{"content": "se contabiliza para cada especie", "x": ["ab"]}]}
