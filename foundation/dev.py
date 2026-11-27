r"""Start the whole platform on this machine with one command: the API (with the workbench) and one
worker process per workflow type, kept running. Ctrl+C stops everything.

    .\.venv\Scripts\python.exe -m foundation.dev                  # workers for every workflow type
    .\.venv\Scripts\python.exe -m foundation.dev --types echo     # only the test workflow

Open http://127.0.0.1:8100/ for the workbench. The schema must exist (python -m foundation.db migrate).
The worker processes are separate, as in production: restarting the API doesn't touch a running task.

A process that exits (a crash, the database gone at start-up) is started again, after 1 s, then 2,
4 … up to 60 s if it keeps failing; a process that has run for 5 minutes counts as healthy again.
A task the crashed worker was running fails with `worker_lost` when its lease runs out, and a person
retries it, as before (design §2).
"""
import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .settings import ROOT

log = logging.getLogger("foundation.dev")
HEALTHY_AFTER_S = 300
MAX_BACKOFF_S = 60


@dataclass
class Proc:
    name: str
    argv: list[str]
    process: object = None
    started: float = 0.0
    failures: int = 0
    restart_at: float | None = None
    restarts: list[float] = field(default_factory=list)


class Supervisor:
    def __init__(self, procs: list[Proc], popen=None, clock=time.monotonic):
        self.procs, self.clock = procs, clock
        self.popen = popen or (lambda argv: subprocess.Popen(argv, cwd=ROOT))

    def start(self) -> None:
        for p in self.procs:
            self._spawn(p)

    def _spawn(self, p: Proc) -> None:
        p.process, p.started, p.restart_at = self.popen(p.argv), self.clock(), None

    def tick(self) -> None:
        """Notice exited processes and start them again when their wait is over."""
        now = self.clock()
        for p in self.procs:
            if p.restart_at is not None:
                if now >= p.restart_at:
                    log.warning("starting %s again (restart %d)", p.name, p.failures)
                    p.restarts.append(now)
                    self._spawn(p)
                continue
            code = p.process.poll()
            if code is None:
                if p.failures and now - p.started >= HEALTHY_AFTER_S:
                    p.failures = 0  # ran long enough: the next crash waits 1 s again
                continue
            p.failures += 1
            wait = min(2 ** (p.failures - 1), MAX_BACKOFF_S)
            p.restart_at = now + wait
            log.error("%s exited with code %s; starting it again in %d s", p.name, code, wait)

    def stop(self) -> None:
        for p in self.procs:
            if p.process is not None and p.process.poll() is None:
                p.process.terminate()
        for p in self.procs:
            if p.process is not None:
                try:
                    p.process.wait(10)
                except subprocess.TimeoutExpired:
                    p.process.kill()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--types", default="clip,echo,report,ingest", help="one worker process per type (comma-separated)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    procs = [Proc(f"worker {t.strip()}", [sys.executable, "-m", "foundation.worker", "--types", t.strip()])
             for t in args.types.split(",") if t.strip()]
    procs.append(Proc("api", [sys.executable, "-m", "foundation.api"]))
    sup = Supervisor(procs)
    sup.start()
    try:
        while True:
            time.sleep(1)
            sup.tick()
    except KeyboardInterrupt:
        return 0
    finally:
        sup.stop()


if __name__ == "__main__":
    raise SystemExit(main())
