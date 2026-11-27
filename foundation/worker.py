r"""The worker process: claim a task, run its workflow, record what happened (design §3, §8).

    .\.venv\Scripts\python.exe -m foundation.worker --types echo

One task at a time per process; run more processes for more parallel tasks. A task waiting for a
person holds no worker: the worker records the question and moves on; whoever answers makes the
task pending again and any worker resumes it. A separate heartbeat
thread renews the lease while the workflow runs, so a long step (a 3-minute render) never loses it.
The same thread enforces the timeout and notices a cancel; the workflow learns about either at its
next `ctx.check()` / `ctx.step()`.

Every loop also reconciles: running tasks whose lease expired (their worker died) are marked
`failed / worker_lost`. Nothing is retried automatically in phases 1–2 (design §2).
"""
import argparse
import contextlib
import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid
from collections.abc import Sequence
from dataclasses import replace

from .artifacts import LocalStorage
from .contracts import BadOutput, Finished, Stop, TaskView, Waiting
from .ops import PRESENCE_S, Ops
from .registry import Registry, UnknownWorkflow, VersionMismatch
from .tasks import TaskRepository
from .uploads import Uploads

log = logging.getLogger("foundation.worker")


class StepContext:
    """What a workflow may use while it runs."""

    def __init__(self, repo: TaskRepository, task: TaskView, owner: str, artifact_dir, uploads=None):
        self.repo, self.task_id, self.owner, self.artifact_dir = repo, task.id, owner, artifact_dir
        self._uploads = uploads
        self._stop: str | None = None
        self._lock = threading.Lock()

    def check(self) -> None:
        """Raise `Stop` if the task must stop: cancelled, timed out or lease lost."""
        if self._stop:
            raise Stop(self._stop)  # type: ignore[arg-type]

    def step(self, name: str, progress: float | None = None) -> None:
        """Record the current step, and check whether to stop."""
        self.check()
        if not self.repo.step(self.task_id, self.owner, name, progress):
            self.request_stop("lease_lost")
            self.check()

    def log(self, kind: str, data: dict) -> None:
        """Add a business event to the task history."""
        if not self.repo.log(self.task_id, self.owner, kind, data):
            self.request_stop("lease_lost")
            self.check()

    def upload_path(self, upload_id: str):
        """The file a person uploaded for this task (an `UploadRef` in the input). Read it; don't move it."""
        return self._uploads.path(upload_id)

    def request_stop(self, reason: str) -> None:
        with self._lock:
            # lease_lost and timeout outrank cancel: after them, nothing more may be written
            if self._stop is None or (self._stop == "cancelled" and reason != "cancelled"):
                self._stop = reason

    @property
    def stop_reason(self) -> str | None:
        return self._stop


class Heartbeat(threading.Thread):
    def __init__(self, repo: TaskRepository, ctx: StepContext, lease_s: float, every_s: float, timeout_s: float):
        super().__init__(name=f"heartbeat-{ctx.task_id}", daemon=True)
        self.repo, self.ctx, self.lease_s, self.every_s, self.timeout_s = repo, ctx, lease_s, every_s, timeout_s
        self.done = threading.Event()

    def run(self) -> None:
        started = last_renewed = time.monotonic()
        while not self.done.wait(self.every_s):
            if time.monotonic() - started > self.timeout_s:
                self.ctx.request_stop("timeout")
                self.repo.fail(self.ctx.task_id, self.ctx.owner, "timeout",
                               f"the step did not finish within {self.timeout_s:g} s")
                return
            try:
                owned, cancel = self.repo.renew(self.ctx.task_id, self.ctx.owner, self.lease_s)
                last_renewed = time.monotonic()
            except Exception as e:  # noqa: BLE001  can't reach the database right now
                # keep trying while the last renewal still holds; give up before the lease runs out,
                # because after that another worker may rightly take the task over
                if time.monotonic() - last_renewed + self.every_s < self.lease_s:
                    log.warning("lease renewal for %s failed (%s); retrying", self.ctx.task_id, type(e).__name__)
                    continue
                log.exception("lease renewal failed for %s until the lease ran out", self.ctx.task_id)
                self.ctx.request_stop("lease_lost")
                return
            if not owned:
                self.ctx.request_stop("lease_lost")
                return
            if cancel:
                self.ctx.request_stop("cancelled")


class Worker:
    def __init__(self, repo: TaskRepository, registry: Registry, storage: LocalStorage, types: Sequence[str], *,
                 lease_s: float = 60, heartbeat_s: float = 10, poll_s: float = 5, reconcile_s: float = 15,
                 name: str | None = None):
        if heartbeat_s * 3 > lease_s:
            raise ValueError("the lease must survive at least two missed heartbeats (lease_s >= 3 * heartbeat_s)")
        self.repo, self.registry, self.storage, self.types = repo, registry, storage, list(types)
        self.lease_s, self.heartbeat_s, self.poll_s, self.reconcile_s = lease_s, heartbeat_s, poll_s, reconcile_s
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self._last_reconcile = 0.0
        self.current_task: str | None = None

    def _presence(self, stop: threading.Event) -> None:
        """Report in every few seconds (and say what it is running), so the workbench can tell a
        quiet queue from a dead worker. Reporting problems never stop the worker."""
        pool = getattr(self.repo, "pool", None)
        if pool is None:
            return
        ops, host = Ops(pool), socket.gethostname()
        while True:
            try:
                ops.seen(self.name, self.types, host, os.getpid(), self.current_task)
            except Exception as e:  # noqa: BLE001
                log.warning("could not report presence (%s)", type(e).__name__)
            if stop.wait(PRESENCE_S):
                break
        with contextlib.suppress(Exception):  # a clean stop is recorded when the database is there
            ops.stopped(self.name)

    def run(self, stop: threading.Event, listener=None) -> None:
        """Loop until `stop` is set: reconcile now and then, claim and run tasks, wait when idle."""
        log.info("worker %s serving %s", self.name, self.types)
        presence = threading.Thread(target=self._presence, args=(stop,), name="presence", daemon=True)
        presence.start()
        try:
            self._loop(stop, listener)
        finally:
            stop.set()
            presence.join(timeout=5)

    def _loop(self, stop: threading.Event, listener) -> None:
        failures = 0
        while not stop.is_set():
            try:
                if time.monotonic() - self._last_reconcile >= self.reconcile_s:
                    self._last_reconcile = time.monotonic()
                    for task_id, status in self.repo.reconcile():
                        log.warning("task %s: lease expired → %s", task_id, status)
                if self.run_once():
                    failures = 0
                    continue
                failures = 0
            except Exception as e:  # noqa: BLE001  e.g. the database is restarting: wait and try again
                failures += 1
                log.warning("worker loop error (%s: %s); retrying", type(e).__name__, e)
                stop.wait(min(self.poll_s * failures, 60))
                continue
            if listener is not None:
                listener.wait(self.poll_s)
            else:
                stop.wait(self.poll_s)

    def run_once(self, task_id: str | None = None) -> bool:
        """Claim one task (or exactly `task_id`) and run it. False when there was nothing to claim."""
        owner = f"{self.name}:{uuid.uuid4().hex[:8]}"  # one owner id per claim, so an old claim can't write
        row = self.repo.claim(self.types, owner, self.lease_s, task_id)
        if row is None:
            return False
        self.current_task = row["id"]
        try:
            self.execute(TaskView.from_row(row), owner)
        finally:
            self.current_task = None
        return True

    def execute(self, task: TaskView, owner: str) -> None:
        try:
            wf = self.registry.load(task.workflow_type, task.workflow_version)
        except (UnknownWorkflow, VersionMismatch) as e:
            self.repo.fail(task.id, owner, "bad_version", str(e))
            return
        ctx = StepContext(self.repo, task, owner, self.storage.dir_for(task.id), Uploads(self.repo.pool, self.storage))
        beat = Heartbeat(self.repo, ctx, self.lease_s, self.heartbeat_s, wf.timeout_s)
        beat.start()
        try:
            if task.command.kind == "resume":
                task = replace(task, resume=self.repo.resume_data(task.command.decision_id))
            result = wf.advance(task, task.command, ctx)
            if isinstance(result, Waiting):
                answered = task.command.decision_id if task.command.kind == "resume" else None
                previews = self.storage.collect(task.id, result.previews)
                if not self.repo.wait(task.id, owner, result, answered, previews):
                    log.warning("task %s paused but its lease was gone (%s); question not opened",
                                task.id, ctx.stop_reason or "expired")
                return
            if not isinstance(result, Finished):
                raise TypeError(f"{task.workflow_type}.advance returned {type(result).__name__}, "
                                "not Finished or Waiting")
            files = self.storage.collect(task.id, result.artifacts)
            if not self.repo.finish(task.id, owner, result.outcome, result.summary, files):
                log.warning("task %s finished but its lease was gone (%s); result not recorded",
                            task.id, ctx.stop_reason or "expired")
        except Stop as s:
            if s.reason == "cancelled":
                self.repo.cancelled(task.id, owner)
            # timeout: already recorded by the heartbeat; lease_lost: no longer ours to write
            log.info("task %s stopped: %s", task.id, s.reason)
        except BadOutput as e:
            self.repo.fail(task.id, owner, "bad_output", str(e))
        except Exception as e:  # noqa: BLE001  any step error fails the task, with the cause recorded
            tb = "".join(traceback.format_exception(e)[-6:])
            self.repo.fail(task.id, owner, "step_error", f"{type(e).__name__}: {e}\n{tb}")
            log.warning("task %s failed: %s: %s", task.id, type(e).__name__, e)
        finally:
            beat.done.set()
            beat.join(timeout=self.heartbeat_s + 5)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exercised by the kill test as a process
    from .db import check, open_pool
    from .dispatch.postgres import PostgresDispatcher
    from .settings import PlatformSettings, valid_schema

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--types", required=True, help="comma-separated workflow types this worker serves")
    p.add_argument("--schema", help="platform schema (default from FOUNDATION_SCHEMA or 'foundation')")
    p.add_argument("--lease", type=float, default=60)
    p.add_argument("--heartbeat", type=float, default=10)
    p.add_argument("--poll", type=float, default=5)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    s = PlatformSettings.load()
    schema = valid_schema(args.schema or s.schema)
    pool = open_pool(s.database_url, schema, max_size=4)
    check(pool)
    dispatcher = PostgresDispatcher(s.database_url, schema)
    worker = Worker(TaskRepository(pool, dispatcher), Registry(), LocalStorage(s.files_dir),
                    [t.strip() for t in args.types.split(",") if t.strip()],
                    lease_s=args.lease, heartbeat_s=args.heartbeat, poll_s=args.poll)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    listener = dispatcher.listener()
    try:
        worker.run(stop, listener)
    finally:
        listener.close()
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
