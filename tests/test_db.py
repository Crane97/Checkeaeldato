"""Conexiones de la pipeline a Supabase: nunca esperan para siempre una respuesta."""

import time

import psycopg
import pytest

from pipeline import db


def test_every_wait_gets_the_default_timeout(monkeypatch):
    seen = {}

    def fake_wait(gen, fileno, interval, timeout):
        seen["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(db.waiting, "wait", fake_wait)
    conn = object.__new__(db.TimedConnection)
    conn.io_timeout = 7.0
    conn.pgconn = type("PG", (), {"socket": 3, "status": psycopg.pq.ConnStatus.OK})()
    assert conn.wait(iter(())) == "ok" and seen["timeout"] == 7.0
    assert conn.wait(iter(()), timeout=2.0) == "ok" and seen["timeout"] == 2.0  # explicit wins


@pytest.mark.supabase
def test_a_query_that_does_not_answer_in_time_fails_instead_of_hanging(supabase_url):
    start = time.monotonic()
    with (
        pytest.raises(psycopg.OperationalError, match="no respondió"),
        db.connect(supabase_url, io_timeout=1) as conn,
    ):
        conn.execute("select pg_sleep(10)")
    assert time.monotonic() - start < 6  # not the 10 s of the query, nor another wait on exit


@pytest.mark.supabase
def test_normal_queries_work(supabase_url):
    with db.connect(supabase_url) as conn:
        assert conn.execute("select 1").fetchone() == (1,)
