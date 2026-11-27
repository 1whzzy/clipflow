"""The material library (phase 4): what ingestion found in replays and product footage, by product.

    python -m clipflow.library migrate        # create or upgrade the tables (schema `library`)
    python -m clipflow.library find 示例裤A    # what the library has for a product

`store.py` owns the tables; `reader.py` is how Clipflow reads them; `api.py` is the workbench's view.
The `ingest` workflow (workflows/ingest.py) fills it.
"""
import os

from .reader import PostgresLibrary
from .store import MIGRATIONS, AssetIn, LibraryStore, Segment

__all__ = ["MIGRATIONS", "AssetIn", "LibraryStore", "PostgresLibrary", "Segment", "library_schema"]


def library_schema() -> str:
    from ..settings import read_env

    return os.environ.get("LIBRARY_SCHEMA") or read_env().get("LIBRARY_SCHEMA") or "library"
