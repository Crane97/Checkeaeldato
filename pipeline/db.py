"""Conexiones de la pipeline a Supabase con un tiempo máximo por operación.

Una conexión del pooler puede quedarse a medias (el pooler pierde la sesión tras un corte
de red o de SSL): el programa esperaría la respuesta para siempre y el análisis, o el
worker, se quedaría colgado sin error. Con `connect`, cada espera de la conexión tiene un
límite (`IO_TIMEOUT`); al superarlo se cierra la conexión y se lanza OperationalError,
el mismo error que ya tratan quienes usan la base (caché solo en disco, base JSON…).
"""

import psycopg
from psycopg import waiting

IO_TIMEOUT = 60.0  # seconds without an answer to any operation: the connection is dead
CONNECT_TIMEOUT = 10


class TimedConnection(psycopg.Connection):
    io_timeout: float = IO_TIMEOUT

    def wait(self, gen, interval: float = 0.1, timeout: float | None = None):
        try:
            return waiting.wait(
                gen,
                self.pgconn.socket,
                interval=interval,
                timeout=self.io_timeout if timeout is None else timeout,
            )
        except psycopg.errors._WaitTimeout as exc:
            self.close()  # mid-operation: unusable, and a rollback would wait again
            raise psycopg.OperationalError(
                f"La base de datos no respondió en {self.io_timeout:g} s; se cierra la conexión"
            ) from exc


def connect(
    url: str, *, io_timeout: float = IO_TIMEOUT, connect_timeout: int = CONNECT_TIMEOUT
) -> TimedConnection:
    conn = TimedConnection.connect(url, connect_timeout=connect_timeout)
    conn.io_timeout = io_timeout
    return conn
