-- Migration 005: which worker processes are alive (design §16). Each worker reports in every few
-- seconds; one that stopped reporting is gone. Lets the workbench say "clip tasks are queued but no
-- clip worker is running" instead of leaving people looking at 排队中.

CREATE TABLE IF NOT EXISTS workers (
    name          text PRIMARY KEY,               -- host:pid
    types         text[] NOT NULL,
    host          text NOT NULL,
    pid           int  NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    current_task  text,                           -- the task it is running now, if any
    stopped_at    timestamptz                     -- set on a clean stop
);
