-- Migration 003: files a question shows the reviewer (phase 3), e.g. the rendered video at the video
-- review, before anything is delivered. Such a file belongs to its waitpoint; delivered files have none.

ALTER TABLE artifacts ADD COLUMN waitpoint_id text REFERENCES waitpoints (id) ON DELETE CASCADE;

CREATE INDEX artifacts_by_waitpoint ON artifacts (waitpoint_id) WHERE waitpoint_id IS NOT NULL;
