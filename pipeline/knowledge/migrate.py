"""Migraciones de la base de conocimiento (db/migrations/NNNN_nombre.sql).

Cada archivo se aplica una sola vez, en orden, dentro de su propia transacción, y queda
registrado en ops.schema_migrations con su checksum: si un archivo ya aplicado cambia, se
avisa en lugar de reaplicarlo (las migraciones aplicadas no se editan; se añade otra).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "db" / "migrations"
_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_LEDGER = """
create schema if not exists ops;
create table if not exists ops.schema_migrations (
    version     text primary key,
    nombre      text not null,
    checksum    text not null,
    aplicada_en timestamptz not null default now()
);
"""


class MigrationError(RuntimeError):
    """A migration file is misnamed, or an applied one was edited afterwards."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()[:16]


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME_RE.match(path.name)
        if match is None:
            raise MigrationError(f"Nombre de migración no válido: {path.name} (NNNN_nombre.sql)")
        migrations.append(Migration(match.group(1), path.name, path.read_text()))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError("Hay dos migraciones con el mismo número")
    return migrations


def pending(conn: psycopg.Connection, migrations: list[Migration]) -> list[Migration]:
    """Migrations not applied yet; fails if an applied one changed on disk."""
    conn.execute(_LEDGER)
    applied = dict(conn.execute("select version, checksum from ops.schema_migrations").fetchall())
    for m in migrations:
        if m.version in applied and applied[m.version] != m.checksum:
            raise MigrationError(
                f"La migración {m.name} ya aplicada ha cambiado: no se edita, se añade otra"
            )
    return [m for m in migrations if m.version not in applied]


def apply(conn: psycopg.Connection, migration: Migration) -> None:
    """Apply one migration and record it (caller decides when to commit). A transaction-level
    advisory lock keeps two processes from applying migrations at the same time."""
    conn.execute("select pg_advisory_xact_lock(727274)")
    conn.execute(migration.sql)
    conn.execute(
        "insert into ops.schema_migrations (version, nombre, checksum) values (%s, %s, %s)",
        (migration.version, migration.name, migration.checksum),
    )
