"""Task records: every state change of a task goes through here (docs/PLATFORM_DESIGN.md §2, §4).

Two kinds of write:
- **By anyone** (API, admin): submit, decide, cancel, retry. They lock the row and check its state.
- **By the owning worker**: progress, wait, finish, fail. Each is fenced: it only applies while the row is
  `running`, owned by this claim and its lease hasn't expired. A worker that lost its lease can
  keep running, but it can no longer change the task or publish a result.

Every change also appends to `task_events`, in the same transaction.
"""
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .contracts import TERMINAL, Command, DecisionView, ErrorKind, Resume, Waiting, WaitpointView
from .dispatch import Dispatcher


class NotFound(LookupError):
    pass


class Conflict(Exception):
    """The request doesn't fit the task's current state (e.g. retry a task that didn't fail)."""


class StaleDecision(Conflict):
    """The decision answers a question that is no longer open (an old browser tab, a second reviewer
    who was slower, a task that moved on). Carries what is open now, if anything."""

    def __init__(self, message: str, status: str, current: dict | None):
        super().__init__(message)
        self.status, self.current = status, current


class InvalidDecision(ValueError):
    """The answer doesn't fit the question (wrong shape, an action that isn't offered …)."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _event(conn, task_id: str, kind: str, data: dict | None = None) -> None:
    conn.execute("INSERT INTO task_events (task_id, kind, data) VALUES (%s, %s, %s)",
                 (task_id, kind, Jsonb(data or {})))


_UNOWN = "worker_id = NULL, lease_expires_at = NULL, heartbeat_at = NULL"
_RELEASE = f"{_UNOWN}, finished_at = now()"


class TaskRepository:
    def __init__(self, pool: ConnectionPool, dispatcher: Dispatcher):
        self.pool, self.dispatcher = pool, dispatcher

    # ---- by anyone ----------------------------------------------------------------------------

    def submit(self, workflow_type: str, workflow_version: int, input: dict, *, created_by: str = "",
               client_request_id: str | None = None, priority: int = 0, engine_ref: dict | None = None
               ) -> tuple[dict, bool]:
        """Record a task and wake a worker, in one transaction. Returns (task, created).

        The same `client_request_id` again returns the first task (a double click, a network retry);
        with a different workflow or input it's a conflict, not a silent reuse."""
        task_id = new_id("task")
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO tasks (id, workflow_type, workflow_version, input, engine_ref, status, command,
                                      created_by, client_request_id, priority)
                   VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)
                   ON CONFLICT (client_request_id) DO NOTHING RETURNING *""",
                (task_id, workflow_type, workflow_version, Jsonb(input), Jsonb(engine_ref or {}),
                 Jsonb(Command(kind="start").model_dump()), created_by, client_request_id, priority)).fetchone()
            if row is None:
                first = conn.execute("SELECT * FROM tasks WHERE client_request_id = %s",
                                     (client_request_id,)).fetchone()
                if first["workflow_type"] != workflow_type or first["input"] != input:
                    raise Conflict(f"client_request_id {client_request_id!r} was already used for another request "
                                   f"(task {first['id']})")
                return first, False
            _event(conn, task_id, "submitted", {"workflow": f"{workflow_type}@{workflow_version}", "by": created_by})
            self.dispatcher.notify(conn, task_id, workflow_type)
            return row, True

    def get(self, task_id: str) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
        if row is None:
            raise NotFound(task_id)
        return row

    def list(self, *, status: str | None = None, workflow_type: str | Sequence[str] | None = None,
             limit: int = 50) -> list[dict]:
        """Newest first. `workflow_type`: one type or several (an app's workflows)."""
        types = [workflow_type] if isinstance(workflow_type, str) else list(workflow_type or [])
        with self.pool.connection() as conn:
            return conn.execute(
                """SELECT * FROM tasks WHERE (%(s)s::text IS NULL OR status = %(s)s)
                                         AND (cardinality(%(t)s::text[]) = 0 OR workflow_type = ANY(%(t)s))
                   ORDER BY created_at DESC LIMIT %(n)s""",
                {"s": status, "t": types, "n": max(1, min(limit, 500))}).fetchall()

    def events(self, task_id: str) -> list[dict]:
        self.get(task_id)
        with self.pool.connection() as conn:
            return conn.execute("SELECT id, at, kind, data FROM task_events WHERE task_id = %s ORDER BY id",
                                (task_id,)).fetchall()

    def artifacts(self, task_id: str) -> list[dict]:
        """Delivered files (not the previews shown at a question)."""
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM artifacts WHERE task_id = %s AND waitpoint_id IS NULL "
                                "ORDER BY created_at, kind, id", (task_id,)).fetchall()

    def previews(self, waitpoint_ids: Sequence[str]) -> dict[str, list[dict]]:
        """Files shown at these questions, by waitpoint id."""
        out: dict[str, list[dict]] = {w: [] for w in waitpoint_ids}
        if waitpoint_ids:
            with self.pool.connection() as conn:
                for a in conn.execute("SELECT * FROM artifacts WHERE waitpoint_id = ANY(%s) ORDER BY created_at, kind",
                                      (list(waitpoint_ids),)).fetchall():
                    out[a["waitpoint_id"]].append(a)
        return out

    def artifact(self, artifact_id: str) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
        if row is None:
            raise NotFound(artifact_id)
        return row

    def request_cancel(self, task_id: str, by: str = "") -> dict:
        """Pending (and later waiting) tasks are cancelled at once. A running task gets a flag, and
        its worker stops it at the next step boundary."""
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
            if row is None:
                raise NotFound(task_id)
            if row["status"] in TERMINAL:
                raise Conflict(f"task {task_id} is already {row['status']}")
            if row["status"] == "running":
                row = conn.execute("UPDATE tasks SET cancel_requested = true, updated_at = now() WHERE id = %s "
                                   "RETURNING *", (task_id,)).fetchone()
                _event(conn, task_id, "cancel_requested", {"by": by})
            else:
                row = conn.execute(f"UPDATE tasks SET status = 'cancelled', outcome = 'cancelled', "
                                   f"updated_at = now(), {_RELEASE} WHERE id = %s RETURNING *", (task_id,)).fetchone()
                conn.execute("UPDATE waitpoints SET status = 'cancelled', closed_at = now(), "
                             "close_reason = 'task cancelled' WHERE task_id = %s AND status = 'open'", (task_id,))
                _event(conn, task_id, "cancelled", {"by": by})
            return row

    # ---- human review ---------------------------------------------------------------------------

    def open_waitpoint(self, task_id: str) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM waitpoints WHERE task_id = %s AND status = 'open'",
                                (task_id,)).fetchone()

    def waitpoints(self, task_id: str) -> list[dict]:
        """Every question asked on this task, oldest first, each with its answer if any."""
        with self.pool.connection() as conn:
            return conn.execute(
                """SELECT w.*, d.id AS decision_id, d.body AS decision_body, d.by AS decided_by,
                          d.at AS decided_at, d.rejected_reason
                   FROM waitpoints w LEFT JOIN decisions d ON d.waitpoint_id = w.id
                   WHERE w.task_id = %s ORDER BY w.opened_at, w.id""", (task_id,)).fetchall()

    def decide(self, task_id: str, waitpoint_id: str, body: dict, *, by: str = "",
               client_request_id: str | None = None,
               check: Callable[[WaitpointView, dict], str | None] | None = None) -> tuple[dict, bool]:
        """Answer the open question; the task becomes pending with `resume`. Returns (decision, created).

        In one transaction, under the task's row lock:
        1. the same `client_request_id` again returns the first decision (a double click, a retry);
        2. the task isn't waiting, or this isn't the open question → `StaleDecision`;
        3. `check` (the workflow's quick check) finds a problem → `InvalidDecision`, nothing changes;
        4. save the answer, close the question, make the task pending, wake a worker."""
        with self.pool.connection() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
            if task is None:
                raise NotFound(task_id)
            if client_request_id:  # checked under the lock, so two identical requests can't both pass
                first = conn.execute("SELECT * FROM decisions WHERE client_request_id = %s",
                                     (client_request_id,)).fetchone()
                if first is not None:
                    if (first["task_id"], first["waitpoint_id"], first["body"]) != (task_id, waitpoint_id, body):
                        raise Conflict(f"client_request_id {client_request_id!r} was already used for another "
                                       f"decision ({first['id']})")
                    return first, False
            wp = conn.execute("SELECT * FROM waitpoints WHERE task_id = %s AND status = 'open'",
                              (task_id,)).fetchone()
            if task["status"] != "waiting" or wp is None or wp["id"] != waitpoint_id:
                raise StaleDecision(f"task {task_id} is not waiting for an answer to {waitpoint_id} "
                                    f"(status {task['status']}, open question: {wp['id'] if wp else 'none'})",
                                    task["status"], wp)
            if check is not None and (problem := check(WaitpointView.from_row(wp), body)):
                raise InvalidDecision(problem)
            decision = conn.execute(
                """INSERT INTO decisions (id, waitpoint_id, task_id, body, by, client_request_id)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (new_id("dec"), wp["id"], task_id, Jsonb(body), by, client_request_id)).fetchone()
            conn.execute("UPDATE waitpoints SET status = 'answered', closed_at = now() WHERE id = %s", (wp["id"],))
            conn.execute("UPDATE tasks SET status = 'pending', command = %s, updated_at = now() WHERE id = %s",
                         (Jsonb(Command(kind="resume", decision_id=decision["id"]).model_dump()), task_id))
            _event(conn, task_id, "decided", {"by": by, "waitpoint": wp["id"], "key": wp["key"],
                                              "decision": decision["id"]})
            self.dispatcher.notify(conn, task_id, task["workflow_type"])
            return decision, True

    def resume_data(self, decision_id: str) -> Resume:
        with self.pool.connection() as conn:
            d = conn.execute("SELECT * FROM decisions WHERE id = %s", (decision_id,)).fetchone()
            if d is None:
                raise NotFound(decision_id)
            w = conn.execute("SELECT * FROM waitpoints WHERE id = %s", (d["waitpoint_id"],)).fetchone()
        return Resume(waitpoint=WaitpointView.from_row(w),
                      decision=DecisionView(id=d["id"], body=d["body"], by=d["by"], at=d["at"]))

    def retry(self, task_id: str, by: str = "") -> dict:
        """A failed task goes back to pending with `continue`: the workflow carries on from its last
        save point. The error stays in the history."""
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
            if row is None:
                raise NotFound(task_id)
            if row["status"] != "failed":
                raise Conflict(f"only a failed task can be retried; task {task_id} is {row['status']}")
            row = conn.execute(
                """UPDATE tasks SET status = 'pending', command = %s, error = NULL, error_kind = NULL,
                          finished_at = NULL, cancel_requested = false, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (Jsonb(Command(kind="continue").model_dump()), task_id)).fetchone()
            _event(conn, task_id, "retry", {"by": by})
            self.dispatcher.notify(conn, task_id, row["workflow_type"])
            return row

    def reconcile(self) -> list[tuple[str, str]]:
        """Running tasks whose lease expired: the worker is gone or stuck. They are not retried
        automatically: the outside effects of the lost attempt are unknown (design §2). A task whose
        cancel was requested simply ends cancelled. Returns [(task_id, new status)]."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""UPDATE tasks SET
                       status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                       outcome = CASE WHEN cancel_requested THEN 'cancelled' ELSE outcome END,
                       error_kind = CASE WHEN cancel_requested THEN NULL ELSE 'worker_lost' END,
                       error = CASE WHEN cancel_requested THEN NULL ELSE
                               'the worker stopped renewing its lease (crashed, killed or cut off); '
                               || 'what the lost attempt did outside the database is unknown' END,
                       updated_at = now(), {_RELEASE}
                    WHERE id IN (SELECT id FROM tasks WHERE status = 'running' AND lease_expires_at <= now()
                                 FOR UPDATE SKIP LOCKED)
                    RETURNING id, status, attempt""").fetchall()
            for r in rows:
                _event(conn, r["id"], "worker_lost" if r["status"] == "failed" else "cancelled",
                       {"attempt": r["attempt"]})
            return [(r["id"], r["status"]) for r in rows]

    # ---- by the owning worker -----------------------------------------------------------------

    def claim(self, types: Sequence[str], owner: str, lease_s: float, task_id: str | None = None) -> dict | None:
        """Atomically take one pending task of these types (or exactly `task_id`). Concurrent workers
        skip rows another is claiming, so a task is claimed once however many workers race for it."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE tasks SET status = 'running', worker_id = %(owner)s, attempt = attempt + 1,
                          heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %(lease)s),
                          started_at = coalesce(started_at, now()), current_step = NULL, updated_at = now()
                   WHERE id = (SELECT id FROM tasks
                               WHERE status = 'pending' AND workflow_type = ANY(%(types)s) AND not_before <= now()
                                 AND (%(id)s::text IS NULL OR id = %(id)s)
                               ORDER BY priority DESC, created_at
                               FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING *""",
                {"owner": owner, "lease": lease_s, "types": list(types), "id": task_id}).fetchone()
            if row:
                _event(conn, row["id"], "claimed", {"worker": owner, "attempt": row["attempt"],
                                                    "command": row["command"]["kind"]})
            return row

    def _owned(self, conn, task_id: str, owner: str, sets: str, params: dict[str, Any]) -> dict | None:
        return conn.execute(
            f"""UPDATE tasks SET {sets}, updated_at = now()
                WHERE id = %(id)s AND status = 'running' AND worker_id = %(owner)s AND lease_expires_at > now()
                RETURNING *""", {"id": task_id, "owner": owner, **params}).fetchone()

    def renew(self, task_id: str, owner: str, lease_s: float) -> tuple[bool, bool]:
        """Extend the lease. Returns (still owned, cancel requested)."""
        with self.pool.connection() as conn:
            row = self._owned(conn, task_id, owner,
                              "heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %(lease)s)",
                              {"lease": lease_s})
            return (row is not None, bool(row and row["cancel_requested"]))

    def step(self, task_id: str, owner: str, name: str, progress: float | None) -> bool:
        with self.pool.connection() as conn:
            row = self._owned(conn, task_id, owner, "current_step = %(step)s, progress = %(p)s",
                              {"step": name, "p": progress})
            if row:
                _event(conn, task_id, "step", {"step": name, "progress": progress})
            return row is not None

    def log(self, task_id: str, owner: str, kind: str, data: dict) -> bool:
        with self.pool.connection() as conn:
            if self._owned(conn, task_id, owner, "heartbeat_at = heartbeat_at", {}) is None:
                return False
            _event(conn, task_id, f"workflow.{kind}", data)
            return True

    def wait(self, task_id: str, owner: str, w: Waiting, answered_decision_id: str | None = None,
             previews: Sequence[dict] = ()) -> bool:
        """The workflow needs a person: open the question and release the worker, in one transaction.
        With `w.error` after a resume, the answer just given was not accepted: that question becomes
        `superseded` and the reason is kept on its decision. The reason is also put in the new
        question's payload as `error` (unless the workflow set one), so every review page can show it."""
        payload = w.payload if not w.error or w.payload.get("error") else {**w.payload, "error": w.error}
        with self.pool.connection() as conn:
            row = self._owned(conn, task_id, owner, f"status = 'waiting', current_step = %(step)s, {_UNOWN}",
                              {"step": f"waiting: {w.key}"})
            if row is None:
                return False
            if w.error and answered_decision_id:
                d = conn.execute("UPDATE decisions SET rejected_reason = %s WHERE id = %s RETURNING waitpoint_id",
                                 (w.error[:2000], answered_decision_id)).fetchone()
                if d:
                    conn.execute("UPDATE waitpoints SET status = 'superseded', close_reason = %s WHERE id = %s",
                                 (w.error[:2000], d["waitpoint_id"]))
                _event(conn, task_id, "decision_not_accepted", {"decision": answered_decision_id,
                                                                "reason": w.error[:500]})
            wp_id = new_id("wp")
            conn.execute(
                """INSERT INTO waitpoints (id, task_id, key, subject_version, payload, actions, decision_schema,
                                           view, engine_key, attempt, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')""",
                (wp_id, task_id, w.key, w.subject_version, Jsonb(payload), Jsonb(w.actions),
                 Jsonb(w.decision_schema) if w.decision_schema is not None else None, w.view, w.engine_key,
                 row["attempt"]))
            for a in previews:
                self._insert_artifact(conn, task_id, a, wp_id)
            _event(conn, task_id, "waiting", {"waitpoint": wp_id, "key": w.key, "version": w.subject_version})
            return True

    @staticmethod
    def _insert_artifact(conn, task_id: str, a: dict, waitpoint_id: str | None = None) -> None:
        conn.execute("""INSERT INTO artifacts (id, task_id, kind, storage, location, sha256, bytes, mime, waitpoint_id)
                        VALUES (%(id)s, %(task_id)s, %(kind)s, %(storage)s, %(location)s, %(sha256)s,
                                %(bytes)s, %(mime)s, %(wp)s)""", {**a, "task_id": task_id, "wp": waitpoint_id})

    def finish(self, task_id: str, owner: str, outcome: str, summary: dict, artifacts: list[dict]) -> bool:
        """Succeeded, with its files, in one transaction: either both are recorded or neither."""
        with self.pool.connection() as conn:
            row = self._owned(conn, task_id, owner,
                              f"status = 'succeeded', outcome = %(o)s, summary = %(s)s, progress = 1, {_RELEASE}",
                              {"o": outcome, "s": Jsonb(summary)})
            if row is None:
                return False
            for a in artifacts:
                self._insert_artifact(conn, task_id, a)
            _event(conn, task_id, "succeeded", {"outcome": outcome, "artifacts": [a["kind"] for a in artifacts]})
            return True

    def fail(self, task_id: str, owner: str, kind: ErrorKind, error: str) -> bool:
        with self.pool.connection() as conn:
            row = self._owned(conn, task_id, owner, f"status = 'failed', error_kind = %(k)s, error = %(e)s, {_RELEASE}",
                              {"k": kind, "e": error[:4000]})
            if row:
                _event(conn, task_id, "failed", {"kind": kind, "error": error[:500]})
            return row is not None

    def cancelled(self, task_id: str, owner: str) -> bool:
        with self.pool.connection() as conn:
            row = self._owned(conn, task_id, owner, f"status = 'cancelled', outcome = 'cancelled', {_RELEASE}", {})
            if row:
                _event(conn, task_id, "cancelled", {"at_step": row["current_step"]})
            return row is not None
