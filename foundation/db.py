"""Connections and the schema.

The schema is created and upgraded by one explicit command, never as a side effect of starting the
API or a worker; those only check that the version matches. Upgrades are the numbered files in
`migrations/`, applied in order, each once.

A business module with its own tables (e.g. Clipflow's material library) keeps its own numbered
files and its own Postgres schema, and uses the same `migrate` / `check` with `files=`.

    python -m foundation.db migrate
"""
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import PlatformSettings, valid_schema

MIGRATIONS = sorted(Path(__file__).with_name("migrations").glob("[0-9][0-9][0-9]_*.sql"))
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaMismatch(RuntimeError):
    pass


def open_pool(url: str, schema: str, max_size: int = 8) -> ConnectionPool:
    """A pool whose connections see only the platform schema (plus `public` for extensions).

    Each connection is checked before it is handed out, so after a database restart the next request
    gets a fresh connection instead of an error. The connections are named after the schema
    (`application_name`), which shows who is connected and lets the tests cut exactly their own."""
    valid_schema(schema)
    return ConnectionPool(url, min_size=1, max_size=max_size, open=True, check=ConnectionPool.check_connection,
                          kwargs={"row_factory": dict_row, "options": f"-c search_path={schema}",
                                  "application_name": f"foundation-{schema}"})


def migrate(url: str, schema: str, to: int | None = None, files: list[Path] = MIGRATIONS) -> int:
    """Create the schema or upgrade it to version `to` (default: all of `files`). Returns the version
    it is at. A database newer than this code is refused, not downgraded."""
    valid_schema(schema)
    latest = len(files)
    to = latest if to is None else to
    with psycopg.connect(url) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"foundation-migrate:{schema}",))
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        current = 0
        if conn.execute("SELECT to_regclass('meta') IS NOT NULL").fetchone()[0]:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            current = int(row[0]) if row else 0
        if current > latest:
            raise SchemaMismatch(f"schema {schema} is at version {current}, newer than this code ({latest})")
        for n, path in enumerate(files[current:to], start=current + 1):
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', %s) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(n),))
        return max(current, to)


def check(pool: ConnectionPool, files: list[Path] = MIGRATIONS) -> None:
    """Refuse to start against a missing or different schema."""
    with pool.connection() as conn:
        if not conn.execute("SELECT to_regclass('meta') IS NOT NULL AS ok").fetchone()["ok"]:
            raise SchemaMismatch("tables not found: run the migrate command first")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if not row or int(row["value"]) != len(files):
            raise SchemaMismatch(f"schema version {row and row['value']}, this code needs {len(files)}")


def drop(url: str, schema: str) -> None:
    """Tests only: remove a throwaway schema."""
    valid_schema(schema)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["migrate"]:
        print("usage: python -m foundation.db migrate")
        return 2
    s = PlatformSettings.load()
    if not s.database_url:
        print("DATABASE_URL is not set (.env)")
        return 2
    migrate(s.database_url, s.schema)
    print(f"schema {s.schema} is at version {SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
