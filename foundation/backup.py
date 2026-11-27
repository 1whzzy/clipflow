r"""Database backups (design §17): everything in the database (platform tasks and reviews, the material
library, Clipflow's checkpoints) in one `pg_dump` file, rotated, and checked by restoring it.

    .\.venv\Scripts\python.exe -m foundation.ops backup                 # keep the newest 14
    .\.venv\Scripts\python.exe -m foundation.ops restore-test <file>    # prove a backup restores

Uses `pg_dump` on this machine if it is installed, else the one inside the Postgres container
(`clipflow-postgres`, or BACKUP_CONTAINER). The file is written under a temporary name, listed
with `pg_restore --list`, and only then given its real name, so a half-written backup never looks
like a good one.

Not in it: files (uploads, deliverables, footage, renders). Those are copied separately; a database
backup alone gives back the records, not the videos.
"""
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

PREFIX = "clipflow-db-"


def _conn(url: str) -> dict:
    d = conninfo_to_dict(url)
    return {"user": d.get("user", "postgres"), "db": d.get("dbname", "postgres"), "password": d.get("password", ""),
            "host": d.get("host", "127.0.0.1"), "port": str(d.get("port", "5432"))}


class PgTools:
    """Runs pg_dump / pg_restore / psql: locally if installed, else in the database's container."""

    def __init__(self, url: str, container: str | None = None):
        self.c = _conn(url)
        self.container = container or os.environ.get("BACKUP_CONTAINER", "clipflow-postgres")
        self.local = shutil.which("pg_dump") is not None

    def argv(self, tool: str, *args: str, db: str | None = None) -> list[str]:
        base = [tool, "-U", self.c["user"], *(["-d", db or self.c["db"]] if tool != "pg_restore" or db else []), *args]
        if self.local:
            return [*base[:1], "-h", self.c["host"], "-p", self.c["port"], *base[1:]]
        return ["docker", "exec", "-i", self.container, *base]

    def run(self, tool: str, *args: str, db: str | None = None, stdin=None, stdout=None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PGPASSWORD": self.c["password"]} if self.local else None
        return subprocess.run(self.argv(tool, *args, db=db), stdin=stdin, stdout=stdout or subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, check=False)

    def sql(self, query: str, db: str | None = None) -> str:
        r = self.run("psql", "-tAX", "-v", "ON_ERROR_STOP=1", "-c", query, db=db)
        if r.returncode:
            raise RuntimeError(f"psql failed: {r.stderr.decode(errors='replace')[:500]}")
        return r.stdout.decode().strip()


def backup(url: str, folder: Path, keep: int = 14, tools: PgTools | None = None) -> Path:
    """Dump the database to `folder`, check the file, keep the newest `keep`. Returns the new file."""
    tools = tools or PgTools(url)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.dump"
    part = final.with_name(final.name + ".part")
    with part.open("wb") as out:
        r = tools.run("pg_dump", "-Fc", "--no-owner", stdout=out)
    if r.returncode:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {r.stderr.decode(errors='replace')[:500]}")
    tables = listed_tables(part, tools)
    if not tables:
        part.unlink(missing_ok=True)
        raise RuntimeError("the dump lists no table data: not keeping it")
    part.replace(final)
    for old in sorted(folder.glob(f"{PREFIX}*.dump"))[:-keep] if keep > 0 else []:
        old.unlink()
    return final


def listed_tables(path: Path, tools: PgTools) -> list[str]:
    """`schema.table` for every table whose data is in the dump."""
    with path.open("rb") as f:
        r = tools.run("pg_restore", "--list", stdin=f)
    if r.returncode:
        raise RuntimeError(f"pg_restore --list failed: {r.stderr.decode(errors='replace')[:500]}")
    return [f"{m[1]}.{m[2]}" for m in re.finditer(r"TABLE DATA (\S+) (\S+) ", r.stdout.decode(errors="replace"))]


def restore_test(url: str, path: Path, tools: PgTools | None = None,
                 counts: tuple[str, ...] = ("foundation.tasks", "library.segments")) -> dict:
    """Restore the dump into a scratch database, count rows in a few tables, drop it again."""
    tools = tools or PgTools(url)
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    tools.sql(f'CREATE DATABASE "{scratch}"')
    try:
        with path.open("rb") as f:
            r = tools.run("pg_restore", "--no-owner", "--exit-on-error", db=scratch, stdin=f)
        if r.returncode:
            raise RuntimeError(f"restore failed: {r.stderr.decode(errors='replace')[:500]}")
        out = {}
        for table in counts:
            schema, _, name = table.partition(".")
            exists = tools.sql(f"SELECT to_regclass('{schema}.{name}') IS NOT NULL", db=scratch) == "t"
            out[table] = int(tools.sql(f"SELECT count(*) FROM {schema}.{name}", db=scratch)) if exists else None
        return out
    finally:
        tools.sql(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
