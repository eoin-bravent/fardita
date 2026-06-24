#!/usr/bin/env python3
"""Add the hybrid-search infrastructure to the existing tables:
  * pgvector extension + embedding column + HNSW index (cosine)
  * a compact fuzzy_text field (citation + title) for trigram typo recovery
Idempotent. Embeddings themselves are populated later by embed_chunks.py."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load_neon import load_env, connect

DIM = 384  # BAAI/bge-small-en-v1.5 (fastembed default, CPU-friendly)

STMTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector({DIM})",
    "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS fuzzy_text text",
    # compact label field: citation + title (NOT the whole body) for malformed-citation recovery
    "UPDATE chunks SET fuzzy_text = coalesce(far_address,'') || ' ' || coalesce(title,'') "
    "WHERE fuzzy_text IS NULL",
    "CREATE INDEX IF NOT EXISTS chunks_fuzzy_trgm ON chunks USING gin (fuzzy_text gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks "
    "USING hnsw (embedding vector_cosine_ops)",
]

conn = connect(load_env()); cur = conn.cursor()
for s in STMTS:
    print("->", " ".join(s.split())[:78])
    cur.execute(s)
conn.commit()

cur.execute("SELECT count(*) FROM chunks")
total = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL")
print(f"\nchunks: {total}   awaiting embedding: {cur.fetchone()[0]}")
cur.close(); conn.close()
