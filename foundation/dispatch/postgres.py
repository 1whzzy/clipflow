"""Option A: Postgres alone. Postgres delivers a NOTIFY only when its transaction commits, so a worker
is never woken for a task that wasn't saved; and the task row is the queue, so a task can't be
saved without being queued. Workers also poll, so a missed notification costs one poll interval."""
import logging
import time

import psycopg
from psycopg import sql

log = logging.getLogger("foundation.dispatch")


class PostgresDispatcher:
    def __init__(self, url: str, schema: str):
        self.url, self.schema = url, schema
        self.channel = f"{schema}_tasks"

    def notify(self, conn, task_id: str, workflow_type: str) -> None:
        conn.execute("SELECT pg_notify(%s, %s)", (self.channel, workflow_type))

    def listener(self) -> PostgresListener:
        return PostgresListener(self.url, self.channel, name=f"foundation-{self.schema}-listener")


class PostgresListener:
    """Wakes a worker when a task is submitted. Losing it only costs speed (workers also poll), so a
    dropped connection is reopened on the next wait instead of stopping the worker."""

    def __init__(self, url: str, channel: str, name: str = "foundation-listener"):
        self.url, self.channel, self.name = url, channel, name
        self.conn = None
        self._connect()

    def _connect(self) -> None:
        self.conn = psycopg.connect(self.url, autocommit=True, application_name=self.name)
        self.conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))

    def wait(self, timeout_s: float) -> None:
        try:
            if self.conn is None or self.conn.closed:
                self._connect()
            for _ in self.conn.notifies(timeout=timeout_s, stop_after=1):
                pass
            # drain anything else that arrived, so one burst of submits is one wake-up
            for _ in self.conn.notifies(timeout=0.01):
                pass
        except psycopg.Error as e:
            log.warning("listener connection lost (%s); polling until it is back", type(e).__name__)
            self.close()
            time.sleep(timeout_s)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


class NoDispatcher:
    """For tests that drive workers by hand: no notifications, workers rely on claiming directly."""

    def notify(self, conn, task_id: str, workflow_type: str) -> None:
        pass

    def listener(self):
        raise NotImplementedError
