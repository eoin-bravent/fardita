-- FAR Subpart 5.2 fixture — Phase 1 schema (lexical + graph; no pgvector yet)
-- Run:  psql -d far_test -f schema.sql

DROP TABLE IF EXISTS relationships CASCADE;
DROP TABLE IF EXISTS chunks CASCADE;
DROP TABLE IF EXISTS content_items CASCADE;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Stable, citable objects. Dense: one row per FAR address (down to the deepest
-- cited paragraph), plus structural part/subpart nodes for the breadcrumb tree.
CREATE TABLE content_items (
    content_item_id TEXT PRIMARY KEY,        -- FAR_5_203, FAR_5_203_a, FAR_5_203_a_1
    item_type       TEXT NOT NULL,           -- part | subpart | section | paragraph
    far_address     TEXT,                    -- 5.203 | 5.203(a) | 5.203(a)(1)
    parent_id       TEXT,                    -- (FK relaxed for bulk corpus load)
    title           TEXT,                    -- section title (null for paragraphs)
    breadcrumb      TEXT,
    depth           INT,
    retrievable     BOOLEAN DEFAULT TRUE     -- part/subpart are structural -> false
);

-- Searchable reading units. Sparse/adaptive: one chunk per section (whole text)
-- and one per top-level lettered paragraph (text includes its sub-items).
CREATE TABLE chunks (
    chunk_id        TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL,           -- (FK relaxed for bulk corpus load)
    far_address     TEXT,
    title           TEXT,
    breadcrumb      TEXT,
    canonical_text  TEXT NOT NULL,           -- exact regulatory text
    enriched_text   TEXT NOT NULL,           -- breadcrumb + title + canonical (feeds search)
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', enriched_text)) STORED
    -- embedding vector(384)  -- Phase 2: add once pgvector + a model are in place
);

CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX chunks_trgm    ON chunks USING gin (canonical_text gin_trgm_ops);
CREATE INDEX chunks_item    ON chunks (content_item_id);

-- Cross-references parsed from <xref> + prose, with the confidence tier from the
-- ingestion doc. to_item has NO foreign key on purpose: it may point at an item
-- outside the loaded set (e.g. a Part, or an un-ingested section) so we can see
-- "cited but not loaded".
CREATE TABLE relationships (
    rel_id          SERIAL PRIMARY KEY,
    from_item       TEXT NOT NULL,           -- (FK relaxed for bulk corpus load)
    to_item         TEXT,
    rel_type        TEXT NOT NULL,           -- references | external_reference
    confidence      TEXT,                    -- high | medium | low | coarse | external
    anchor_text     TEXT,                    -- displayed link text, e.g. "5.202"
    target_raw      TEXT,                    -- raw href or prose snippet
    review_required BOOLEAN DEFAULT FALSE
);
CREATE INDEX rel_from ON relationships (from_item);
CREATE INDEX rel_to   ON relationships (to_item);

-- Hierarchy (parent_of) is carried by content_items.parent_id; query it with a
-- recursive CTE rather than duplicating it as relationship rows.
