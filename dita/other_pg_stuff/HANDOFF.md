# FAR Retrieval — Project Handoff (for a fresh Claude Code session)

## What this is
A hybrid-search + graph-RAG system over the **Federal Acquisition Regulation (FAR)**.
Source = DITA XML (~3,900 `.dita` files). Data is loaded into **Neon** (cloud
Postgres + pgvector). A **Spring AI** app (Claude) answers questions grounded in
retrieved FAR text.

## Paths on this (target) computer
- **Project root:** `C:\Coding\dita-analysis\FARDITA\dita`
- DITA corpus (~3,900 `.dita` files): directly in that root.
- Python ingestion tools: `C:\Coding\dita-analysis\FARDITA\dita\_pgtest`
- Spring app: `C:\Coding\dita-analysis\FARDITA\dita\far-api`
- Neon connection string: `_pgtest\.env` (`DATABASE_URL=...`).
(Relative paths elsewhere in this file are under that project root.)

## Current state (already done — data lives in Neon, reachable from any machine)
- **content_items: 26,961** — dense, one row per FAR paragraph address. IDs like
  `FAR_5_203`, `FAR_5_203_a`, `FAR_5_203_a_1`, built from `<ph props="autonumber">`
  (NOT the XML `id` attribute).
- **chunks: 11,252** — section + top-level-letter granularity. Columns: `canonical_text`,
  `enriched_text`, generated `tsv` (tsvector), `fuzzy_text` (trigram), `embedding vector(384)`.
- **relationships: 9,936** — typed cross-refs. `confidence` ∈ high/medium/low/coarse/external.
  medium = ambiguous "5.202(a)(2)"-style links resolved to a paragraph item; low = prose
  ranges ("(a) through (d)"), flagged `review_required`.
- **Lexical lanes live**: tsvector (GIN) + pg_trgm fuzzy. **Vector lane**: pgvector + HNSW
  index added; embeddings populated with **fastembed `BAAI/bge-small-en-v1.5` (384-dim)**.
  Embedding is resumable (`WHERE embedding IS NULL`). Progress:
  `SELECT count(*) FILTER (WHERE embedding IS NOT NULL) FROM chunks;`
- DB size ~57 MB (Neon free tier ~0.5 GB).

## Two code sides — they only meet at the Neon database
- **`_pgtest/`** (Python, ingestion). Use **Python 3.12** for fastembed (3.14 lacks wheels);
  driver is **pg8000** (pure-Python). Scripts: `parse_to_sql.py` (DITA→data, stdlib),
  `load_neon.py` (schema + bulk load; `FAR_ALL=1` = whole corpus), `add_vectors.py`,
  `embed_chunks.py`, `hybrid_search.py`, `q.py "SQL"` (ad-hoc). `.env` holds `DATABASE_URL`
  (gitignored). **Querying/embedding needs NO dita files** — only re-ingestion does.
- **`far-api/`** (Java / Spring AI, the app). Reads Neon only. Keyword + graph retrieval via
  `JdbcTemplate`; Claude via Spring AI. Endpoints `/search`, `/ask`, `/refs`. See
  `far-api/README.md`.

## Immediate task: get far-api running
1. **Merge** the start.spring.io download (it has `mvnw` + a correct `pom.xml`) with the
   scaffolded code: copy `far-api/src/main/java/com/far/farapi/{FarRetriever,FarRag,FarController}.java`
   into the download's matching package folder (fix the `package` line to match), and copy
   `far-api/src/main/resources/application.properties`. Keep the download's own main class.
2. **Env vars**: `ANTHROPIC_API_KEY` (console.anthropic.com), `NEON_PASSWORD` (from `_pgtest/.env`).
3. **Run**: `./mvnw spring-boot:run`. Hit `/search?q=synopsis` first (proves DB), then `/ask?q=...`.
4. **Prereqs**: JDK 21, Maven (or the wrapper), VS Code/Cursor Java + Spring Boot extension packs.

## Backlog / next steps
- **Vector lane + RRF** in far-api (port `_pgtest/hybrid_search.py`). CRITICAL: embed queries
  with the SAME model (`bge-small-en-v1.5`) as ingest — NOT Spring AI's default MiniLM.
- Feed graph neighbours (`references()`) into the prompt as "referenced authority".
- Ingestion gaps: 536 skipped non-`<concept>` topics (subpart intros, `FARmatrix` tables);
  ~17% of edges dangle; em-dash encoding nit (— renders as �).
- Optional: load the 12 eval questions as a table to score retrieval.

## Security
Neon password + Anthropic key are secrets — keep out of git. The Neon password was shared in
chat during setup; rotate it in the Neon console and update `_pgtest/.env`.
