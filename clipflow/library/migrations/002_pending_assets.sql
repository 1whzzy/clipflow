-- Migration 002: a file is registered (`pending`) as soon as ingestion asks a person about it, so the
-- review page can play it; recall still sees only `processed` files.
ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_status_check;
ALTER TABLE assets ADD CONSTRAINT assets_status_check CHECK (status IN ('pending', 'processed', 'rejected'));
