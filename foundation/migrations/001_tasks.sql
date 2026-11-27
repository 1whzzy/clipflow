-- Migration 001: tasks, their history and delivered files (phase 1, slice 1).
-- Applied by `python -m foundation.db migrate` inside the platform's own Postgres schema.

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                text PRIMARY KEY,
    workflow_type     text NOT NULL,
    workflow_version  int  NOT NULL,
    input             jsonb NOT NULL,
    engine_ref        jsonb NOT NULL DEFAULT '{}',          -- e.g. {"thread_id": …} for Clipflow
    status            text NOT NULL CHECK (status IN ('pending', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')),
    command           jsonb NOT NULL,                       -- {"kind": "start" | "continue" | "resume", …}
    outcome           text,
    summary           jsonb,
    current_step      text,
    progress          real,
    error             text,
    error_kind        text CHECK (error_kind IN ('step_error', 'timeout', 'worker_lost', 'bad_version', 'bad_output')),
    attempt           int  NOT NULL DEFAULT 0,
    cancel_requested  boolean NOT NULL DEFAULT false,
    worker_id         text,                                 -- the current owner; set only while running
    heartbeat_at      timestamptz,
    lease_expires_at  timestamptz,
    priority          int  NOT NULL DEFAULT 0,
    not_before        timestamptz NOT NULL DEFAULT now(),
    created_by        text NOT NULL DEFAULT '',
    client_request_id text UNIQUE,                          -- the same submit twice creates one task
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz,
    -- a running task always has an owner and a lease; any other state has neither
    CONSTRAINT running_has_owner CHECK ((status = 'running') = (worker_id IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CONSTRAINT failed_has_kind CHECK ((status = 'failed') = (error_kind IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS tasks_to_claim ON tasks (workflow_type, priority DESC, created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS tasks_leases ON tasks (lease_expires_at) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS tasks_by_time ON tasks (created_at DESC);

CREATE TABLE IF NOT EXISTS task_events (
    id       bigserial PRIMARY KEY,
    task_id  text NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    kind     text NOT NULL,
    data     jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS task_events_by_task ON task_events (task_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id         text PRIMARY KEY,
    task_id    text NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    kind       text NOT NULL,
    storage    text NOT NULL,                               -- local now; object storage later
    location   text NOT NULL,                               -- relative to the storage root
    sha256     text NOT NULL,
    bytes      bigint NOT NULL,
    mime       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artifacts_by_task ON artifacts (task_id);
