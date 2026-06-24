# FAR retrieval test bed (Neon + pgvector)

The data lives in **Neon (cloud Postgres)** — the whole FAR as content_items,
chunks (lexical + vector), and a typed cross-reference graph. Any machine with
the connection string can use it; no local Postgres needed.

## Setup on a new machine
1. Install **Python 3.12** (not 3.14 — fastembed's deps don't have 3.14 wheels yet).
2. From this folder:
   ```
   py -3.12 -m pip install -r requirements.txt
   ```
3. Create a `.env` here (it's gitignored — copy the line by hand):
   ```
   DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require
   ```
4. Verify the connection — instantly hits the populated cloud DB:
   ```
   py -3.12 q.py "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"
   ```

> To **query or embed**, that's all you need — the DITA files are NOT required,
> since the text already lives in Neon. You only need the `../*.dita` corpus if
> you want to re-parse / re-chunk / reload from scratch.

## Scripts
| file | what it does | interpreter |
|------|--------------|-------------|
| `q.py "SQL"` | run any SQL against Neon, print rows | 3.12 or 3.14 |
| `parse_to_sql.py` | DITA → in-memory items/chunks/rels (stdlib only); `FAR_ALL=1` for whole corpus | any |
| `load_neon.py` | parse + (re)build schema + bulk load; `FAR_ALL=1` for whole corpus | 3.12 |
| `corpus_stats.py` | dry-run parse stats, no DB | any |
| `add_vectors.py` | add pgvector column + HNSW index + fuzzy_text | 3.12 |
| `embed_chunks.py` | embed chunks on CPU, write vectors (resumable: only `embedding IS NULL`) | 3.12 |
| `hybrid_search.py "question"` | vector + keyword RRF fusion + graph expansion | 3.12 |
| `schema.sql` / `seed.sql` / `queries.sql` | schema, generated seed, sample queries | — |

## Reset / reload
`load_neon.py` drops and recreates the tables every run, so it's idempotent.
After a reload, rerun `add_vectors.py` then `embed_chunks.py`.
