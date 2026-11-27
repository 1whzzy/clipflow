-- Migration 001: the material library (phase 4). Clipflow's own tables, in their own Postgres schema
-- (`library`, or LIBRARY_SCHEMA), applied by `python -m clipflow.library migrate`.
-- Filled by the `ingest` workflow; read by Clipflow (PostgresLibrary) and the workbench's 素材库 page.

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

-- One row per media file that was ingested. `path` is what Clipflow calls the asset id: relative to
-- the repo root when the file is inside it, else absolute (a shared drive).
CREATE TABLE IF NOT EXISTS assets (
    id            text PRIMARY KEY,
    path          text NOT NULL UNIQUE,
    kind          text NOT NULL CHECK (kind IN ('replay', 'footage')),
    bytes         bigint,
    mtime         double precision,                  -- with bytes: "is this still the file we ingested?"
    duration_ms   int,
    product       text,                              -- footage: the product it shows (from the folder)
    color         text,                              -- footage: the colour (from the folder)
    status        text NOT NULL CHECK (status IN ('processed', 'rejected')),
    task_id       text NOT NULL,                     -- the ingest task that last processed it
    processed_by  text NOT NULL DEFAULT '',          -- who confirmed it ('' = no review)
    processed_at  timestamptz NOT NULL DEFAULT now()
);

-- A stretch of a file, for one product. `sell_block`: the anchor sells the product in a replay.
-- `shot`: product footage that shows the product. Recall = "all segments of product X".
CREATE TABLE IF NOT EXISTS segments (
    id            text PRIMARY KEY,
    asset_id      text NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
    product       text NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('sell_block', 'shot')),
    start_ms      int  NOT NULL CHECK (start_ms >= 0),
    end_ms        int  NOT NULL,
    shot_type     text,
    color         text,
    quality       text CHECK (quality IN ('good', 'ok', 'bad')),
    confidence    text CHECK (confidence IN ('high', 'medium', 'low')),
    evidence      jsonb NOT NULL DEFAULT '[]',       -- sell_block: exact quotes from the transcript
    text          text NOT NULL DEFAULT '',          -- sell_block: what the anchor says in it
    frames        jsonb NOT NULL DEFAULT '[]',       -- shot: the frame tags it was built from
    confirmed     boolean NOT NULL DEFAULT false,    -- a person checked it
    confirmed_by  text NOT NULL DEFAULT '',
    task_id       text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ordered CHECK (end_ms > start_ms)
);

CREATE INDEX IF NOT EXISTS segments_by_product ON segments (product, kind);
CREATE INDEX IF NOT EXISTS segments_by_asset ON segments (asset_id);
