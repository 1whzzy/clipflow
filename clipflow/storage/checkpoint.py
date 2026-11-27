"""The checkpointer: saves the graph state after every step, so a paused or crashed run resumes
from where it stopped instead of starting over.

Postgres in production (docker-compose.yml). In-memory only for tests: a restart loses paused runs.
"""
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver


@contextmanager
def open_checkpointer(database_url: str | None, name: str = "clipflow-checkpoints") -> Iterator[BaseCheckpointSaver]:
    """On a small pool that checks each connection before use: a long-lived worker survives a
    database restart (the step running at that moment fails and can be retried; later ones work).
    `name` is the connections' application_name."""
    if not database_url:
        yield InMemorySaver()
        return
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(database_url, min_size=1, max_size=4, open=True, check=ConnectionPool.check_connection,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                                  "application_name": name})
    try:
        saver = PostgresSaver(pool)
        saver.setup()  # creates the checkpoint tables; safe to run again
        yield saver
    finally:
        pool.close()
