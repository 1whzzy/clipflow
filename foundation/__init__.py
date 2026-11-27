"""The base platform: runs registered workflows as tasks (docs/PLATFORM_DESIGN.md).

It owns what every workflow needs: recording tasks, handing them to separate worker processes,
leases and crash handling, status and history, and delivered files. It knows nothing about video:
nothing in this package may import `clipflow` or `workflows` (tests/test_foundation.py checks it).

    python -m foundation.db migrate                  # create or check the tables
    python -m foundation.worker --types echo         # a worker process
    python -m foundation.api                         # http://127.0.0.1:8100/docs
"""
