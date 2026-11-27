import sys

from foundation import db

from ..settings import Settings
from . import MIGRATIONS, LibraryStore, library_schema


def main(argv: list[str]) -> int:
    url, schema = Settings.load().database_url, library_schema()
    if not url:
        print("DATABASE_URL is not set (.env)")
        return 2
    if argv == ["migrate"]:
        version = db.migrate(url, schema, files=MIGRATIONS)
        print(f"schema {schema} is at version {version}")
        return 0
    if len(argv) == 2 and argv[0] == "find":
        store = LibraryStore.open(url, schema)
        rows = store.segments(argv[1])
        for r in rows:
            what = r["shot_type"] or "讲解"
            mark = "✓" if r["confirmed"] else " "
            print(f"{mark} {r['kind']:<10} {what:<12} {r['color'] or '':<6} {_t(r['start_ms'])}–{_t(r['end_ms'])}  "
                  f"{r['path']}")
        print(f"{len(rows)} segments for {argv[1]}")
        return 0
    print("usage: python -m clipflow.library migrate | find <款名>")
    return 2


def _t(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
