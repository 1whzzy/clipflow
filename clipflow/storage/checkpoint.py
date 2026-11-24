"""The checkpointer: saves the graph state after every step, so a paused or crashed run resumes
from where it stopped instead of starting over.

Postgres in production (docker-compose.yml). In-memory only for tests: a restart loses paused runs.
"""
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver


@contextmanager
def open_checkpointer(database_url: str | None) -> Iterator[BaseCheckpointSaver]:
    if not database_url:
        yield InMemorySaver()
        return
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(database_url) as saver:
        saver.setup()  # creates the checkpoint tables; safe to run again
        yield saver
