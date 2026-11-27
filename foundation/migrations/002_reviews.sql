-- Migration 002: human review (phase 1, slice 2). A waitpoint is one question to a person, bound to
-- one task and the version of what they look at; a decision is the answer (design §4).

CREATE TABLE waitpoints (
    id              text PRIMARY KEY,
    task_id         text NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    key             text NOT NULL,                          -- e.g. confirm_plan, review_video
    subject_version int  NOT NULL DEFAULT 0,                -- the plan / timeline / draft version shown
    payload         jsonb NOT NULL,                         -- what the review page shows
    actions         jsonb NOT NULL DEFAULT '[]',
    decision_schema jsonb,                                  -- JSON Schema of the answer, for the page
    view            text NOT NULL DEFAULT 'generic',        -- which page renders it
    engine_key      text NOT NULL DEFAULT '',               -- the workflow's own id for this pause
    attempt         int  NOT NULL,                          -- the task attempt that opened it
    status          text NOT NULL CHECK (status IN ('open', 'answered', 'superseded', 'cancelled')),
    close_reason    text,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz
);

-- at most one open question per task
CREATE UNIQUE INDEX waitpoints_one_open ON waitpoints (task_id) WHERE status = 'open';
CREATE INDEX waitpoints_by_task ON waitpoints (task_id, opened_at);

CREATE TABLE decisions (
    id                text PRIMARY KEY,
    waitpoint_id      text NOT NULL UNIQUE REFERENCES waitpoints (id) ON DELETE CASCADE,  -- one answer per question
    task_id           text NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    body              jsonb NOT NULL,
    by                text NOT NULL DEFAULT '',
    at                timestamptz NOT NULL DEFAULT now(),
    client_request_id text UNIQUE,                          -- the same submit twice is one decision
    rejected_reason   text                                  -- set when the workflow did not accept it
);
