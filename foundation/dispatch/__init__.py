"""How a worker learns there is work (docs/PLATFORM_DESIGN.md §7).

Only this package knows which option is in use. Tasks, leases, recovery and reviews are the same
code either way, because Postgres holds the task state in both.

- `postgres.PostgresDispatcher` (option A, now): NOTIFY inside the submitting transaction; workers
  LISTEN and also poll, then claim with `FOR UPDATE SKIP LOCKED`.
- an ARQ dispatcher (option B, later): enqueue the task id after commit, re-send pending rows
  periodically; the ARQ job calls the same `Worker.execute`.
"""
from typing import Protocol


class Dispatcher(Protocol):
    def notify(self, conn, task_id: str, workflow_type: str) -> None:
        """Called inside the transaction that made the task pending."""

    def listener(self) -> Listener:
        """A per-worker handle to wait for work."""


class Listener(Protocol):
    def wait(self, timeout_s: float) -> None:
        """Return when there may be work, or after `timeout_s` at most."""

    def close(self) -> None: ...
