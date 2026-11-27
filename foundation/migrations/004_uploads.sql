-- Migration 004: files people upload for a workflow to work on (a replay, product footage, a document).
-- Received in chunks, so a dropped connection resumes where it stopped instead of starting over.
-- A task's input names an upload by id; the worker reads the file from the platform's storage.

CREATE TABLE IF NOT EXISTS uploads (
    id            text PRIMARY KEY,
    name          text NOT NULL,                  -- the file's own name, as the person's computer had it
    bytes         bigint NOT NULL CHECK (bytes >= 0),
    received      bigint NOT NULL DEFAULT 0,
    mime          text NOT NULL DEFAULT 'application/octet-stream',
    sha256        text,                           -- set when complete
    location      text NOT NULL,                  -- under the storage root
    status        text NOT NULL CHECK (status IN ('receiving', 'complete')),
    created_by    text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    CONSTRAINT not_past_the_end CHECK (received <= bytes)
);
