r"""Keeping the platform running (design §16): which workers are alive, what the queues look like,
what needs someone's attention, and cleaning up files nobody will use.

    GET /api/v1/health                                   the workbench's 运行状态 page reads this
    .\.venv\Scripts\python.exe -m foundation.ops cleanup            # list what would be removed
    .\.venv\Scripts\python.exe -m foundation.ops cleanup --apply    # remove it
    .\.venv\Scripts\python.exe -m foundation.ops backup             # the database, rotated (backup.py)
    .\.venv\Scripts\python.exe -m foundation.ops restore-test FILE  # prove a backup restores
"""
import argparse
import os
import shutil
import threading
import sys
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import LocalStorage

PRESENCE_S = 10.0  # how often a worker reports in
GONE_AFTER_S = 3 * PRESENCE_S  # silent this long = not running
STUCK_PENDING_S = 600  # a task queued this long with a live worker for its type is worth a look
CHECK_EVERY_S = 300  # module checks (the model gateway costs a request each time)
MIN_FREE_GB = 20  # an hour of replay is 1.5 GB; renders and uploads add up


def disk_check(root: Path) -> dict:
    """Free space where uploads and deliverables are written."""
    try:
        free = shutil.disk_usage(root).free / 1024 ** 3
    except OSError as e:
        return {"name": "存储空间", "ok": False, "message": f"存储目录不可用：{e}"}
    ok = free >= MIN_FREE_GB
    return {"name": "存储空间", "ok": ok,
            "message": f"{root.drive or root} 还剩 {free:.0f} GB" + ("" if ok else "：快满了，上传和出片会失败")}


class CheckRunner:
    """Runs the registered checks in the background, so the health page never waits for a slow one."""

    def __init__(self, checks: dict, every_s: float = CHECK_EVERY_S, storage_root: Path | None = None):
        self.checks, self.every_s, self.storage_root = checks, every_s, storage_root
        self.results: list[dict] = []
        self.checked_at: str | None = None
        self._stop = threading.Event()

    def run_now(self) -> list[dict]:
        out = [disk_check(self.storage_root)] if self.storage_root else []
        for name, fn in self.checks.items():
            try:
                out += [{**r, "module": name} for r in fn()]
            except Exception as e:  # noqa: BLE001  a broken check is reported, never fatal
                out.append({"name": name, "module": name, "ok": False, "message": f"检查出错：{type(e).__name__}: {e}"})
        self.results, self.checked_at = out, datetime.now(UTC).isoformat()
        return out

    def start(self) -> CheckRunner:
        def loop():
            while not self._stop.is_set():
                self.run_now()
                self._stop.wait(self.every_s)

        threading.Thread(target=loop, name="health-checks", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()


class Ops:
    def __init__(self, pool):
        self.pool = pool

    # ---- worker presence ---------------------------------------------------------------------

    def seen(self, name: str, types: list[str], host: str, pid: int, current_task: str | None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO workers (name, types, host, pid, current_task) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET types = excluded.types, last_seen = now(),
                       current_task = excluded.current_task, stopped_at = NULL""",
                (name, types, host, pid, current_task))

    def stopped(self, name: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE workers SET stopped_at = now(), current_task = NULL WHERE name = %s", (name,))

    def workers(self, include_gone_for_s: float = 24 * 3600) -> list[dict]:
        """Workers seen recently, each with `alive`."""
        with self.pool.connection() as conn:
            return conn.execute(
                """SELECT *, (stopped_at IS NULL AND last_seen > now() - make_interval(secs => %s)) AS alive,
                          extract(epoch FROM now() - last_seen) AS silent_s
                   FROM workers WHERE last_seen > now() - make_interval(secs => %s)
                   ORDER BY alive DESC, name""", (GONE_AFTER_S, include_gone_for_s)).fetchall()

    # ---- health ------------------------------------------------------------------------------------

    def health(self, types: list[str], checks: list[dict] = (), checked_at: str | None = None) -> dict:
        """Queues per workflow type, live workers, recent failures, the latest checks (disk, model
        gateway …), and warnings in plain words."""
        with self.pool.connection() as conn:
            queues = {r["workflow_type"]: r for r in conn.execute(
                """SELECT workflow_type,
                          count(*) FILTER (WHERE status = 'pending') AS pending,
                          count(*) FILTER (WHERE status = 'running') AS running,
                          count(*) FILTER (WHERE status = 'waiting') AS waiting,
                          extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'pending'))
                              AS oldest_pending_s,
                          count(*) FILTER (WHERE status = 'failed' AND finished_at > now() - interval '24 hours')
                              AS failed_24h
                   FROM tasks GROUP BY workflow_type""").fetchall()}
            failures = conn.execute(
                """SELECT error_kind, count(*) AS n FROM tasks
                   WHERE status = 'failed' AND finished_at > now() - interval '24 hours'
                   GROUP BY error_kind ORDER BY n DESC""").fetchall()
            uploads = conn.execute(
                "SELECT count(*) AS n FROM uploads WHERE status = 'receiving'").fetchone()["n"]
        workers = self.workers()
        alive = [w for w in workers if w["alive"]]
        out, warnings = [], []
        for t in sorted(set(types) | set(queues)):
            q = queues.get(t, {})
            serving = [w["name"] for w in alive if t in w["types"]]
            row = {"type": t, "pending": q.get("pending", 0), "running": q.get("running", 0),
                   "waiting": q.get("waiting", 0), "failed_24h": q.get("failed_24h", 0),
                   "oldest_pending_s": round(q["oldest_pending_s"]) if q.get("oldest_pending_s") else None,
                   "workers": serving, "registered": t in types}
            out.append(row)
            if row["pending"] and not serving:
                warnings.append({"type": t, "kind": "no_worker",
                                 "message": f"有 {row['pending']} 个 {t} 任务在排队，但没有处理 {t} 的执行进程在运行"})
            elif row["oldest_pending_s"] and row["oldest_pending_s"] > STUCK_PENDING_S:
                warnings.append({"type": t, "kind": "slow_queue",
                                 "message": f"{t} 任务已排队 {row['oldest_pending_s'] // 60} 分钟"
                                            f"（{len(serving)} 个执行进程都在忙）"})
            if row["waiting"]:
                warnings.append({"type": t, "kind": "waiting_review",
                                 "message": f"{row['waiting']} 个 {t} 任务等人审核"})
        warnings += [{"type": None, "kind": "check", "message": f"{c['name']}：{c['message']}"}
                     for c in checks if not c["ok"]]
        return {"checked_at": datetime.now(UTC).isoformat(), "database": "ok", "queues": out,
                "checks": list(checks), "checks_at": checked_at,
                "workers": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in w.items()}
                            for w in workers],
                "failures_24h": failures, "uploads_receiving": uploads, "warnings": warnings}

    # ---- cleanup -----------------------------------------------------------------------------------

    def cleanup(self, storage: LocalStorage, apply: bool = False, abandoned_h: float = 24,
                unused_days: float = 30) -> list[dict]:
        """Uploads nobody finished (no chunk for `abandoned_h` hours) and finished uploads no task has
        used for `unused_days` days: their files and rows. A dry run unless `apply`."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT u.id, u.name, u.location, u.status, u.received, u.created_at,
                          CASE WHEN u.status = 'receiving' THEN 'abandoned' ELSE 'unused' END AS why
                   FROM uploads u
                   WHERE (u.status = 'receiving' AND u.created_at < now() - make_interval(hours => %s))
                      OR (u.status = 'complete' AND u.completed_at < now() - make_interval(days => %s)
                          AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.input::text LIKE '%%' || u.id || '%%'))
                   ORDER BY u.created_at""", (int(abandoned_h), int(unused_days))).fetchall()
            if apply:
                for r in rows:
                    folder = storage.path(r["location"]).parent
                    if folder.name == r["id"]:  # uploads/<id>/<name>: remove the whole folder
                        shutil.rmtree(folder, ignore_errors=True)
                    conn.execute("DELETE FROM uploads WHERE id = %s", (r["id"],))
        return rows


def main(argv: list[str] | None = None) -> int:
    from . import db
    from .settings import PlatformSettings

    p = argparse.ArgumentParser(prog="python -m foundation.ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cleanup", help="remove abandoned and unused uploads")
    c.add_argument("--apply", action="store_true", help="really remove (default: only list)")
    c.add_argument("--abandoned-hours", type=float, default=24)
    c.add_argument("--unused-days", type=float, default=30)
    b = sub.add_parser("backup", help="dump the database to the backup folder and keep the newest N")
    b.add_argument("--keep", type=int, default=14)
    b.add_argument("--dir", help="backup folder (default FOUNDATION_BACKUP_DIR or backups/)")
    r = sub.add_parser("restore-test", help="restore a backup into a scratch database and count rows")
    r.add_argument("file")
    args = p.parse_args(argv)
    s = PlatformSettings.load()
    if args.cmd in ("backup", "restore-test"):
        from . import backup as bk
        from .settings import ROOT

        if args.cmd == "backup":
            folder = Path(args.dir or os.environ.get("FOUNDATION_BACKUP_DIR") or ROOT / "backups")
            path = bk.backup(s.database_url, folder, args.keep)
            print(f"backup written: {path} ({path.stat().st_size / 1048576:.1f} MB)")
        else:
            print(bk.restore_test(s.database_url, Path(args.file)))
        return 0
    pool = db.open_pool(s.database_url, s.schema, max_size=2)
    db.check(pool)
    rows = Ops(pool).cleanup(LocalStorage(s.files_dir), args.apply, args.abandoned_hours, args.unused_days)
    for r in rows:
        print(f"{'removed' if args.apply else 'would remove'} {r['id']} ({r['why']}): {r['name']}")
    print(f"{len(rows)} upload(s)" + ("" if args.apply else "; run with --apply to remove them"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
