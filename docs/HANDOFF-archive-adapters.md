# Handoff: build acquisition.gov archive adapters for the versioned store

## Mission
Extend the FAR versioned store further back in time than the GitHub history (which starts at
FAC 2023-02) by writing **per-era adapters** for the acquisition.gov FAR archives
(https://www.acquisition.gov/archives). The user has a local folder of downloaded archives.

## What already exists (do not rebuild)
All in `pipeline/` — read `pipeline/README.md` (sections "Versioned store" and "Change tracking")
before writing any code:

- **`store.py`** — the SCD-Type-2 versioned store + `Store.merge_snapshot()`. One merge operation
  handles forward updates AND backfill (snapshots older than, between, or after known editions).
  Idempotent, order-independent. Temporal fields: `effective_from`/`effective_to` (legal dates,
  half-open `[from,to)`, `null` = in force), `current`, `content_hash`, `ingested_at`, `source`,
  `source_commit`, `last_seen_version`/`last_seen_date`.
- **`chunker.py`** + `../extract_json.py` — the GitHub-DITA adapter: DITA file → chunk rows.
  Identity = `(citation, alternate)`; citation comes from the autonumber INSIDE the file (never
  trust filenames). `bottom_level=paragraph` by default.
- **`update.py`** — `update` (daily GSA-repo ingest) and `replay` (historical GitHub editions).
  `ingest_tree()` shows the canonical ingest sequence: chunk → merge_snapshot → changelog → report.
- **`verify_store.py`** — invariants + as-of spot checks. **`store_coverage.py`** — completeness
  proofs (`--snapshot` mode: store as-of == full re-chunk of an edition's tree).
- **`store/`** — the live store: 18 editions, FAC 2023-02 (2023-03-16) → FAC 2026-01 (2026-03-13),
  14,245 rows, all current rows LLM-verified (`refs_verified_from`). Do not regenerate it;
  archive editions BACKFILL into it (merge_snapshot handles `D` older than everything: identical
  text extends a row's `effective_from` floor down; different text inserts closed rows).

## The adapter contract
An adapter converts one archive edition into: `(chunk_rows, effective_date, source_version)`,
then calls `Store.merge_snapshot(rows, effective_date, source_version,
source="acquisition-gov-archive", source_commit=<archive filename/id>)`.
Chunk rows must match the standard shape (see `demo_fields.py` / `VERIFIED_FORMAT.md`); fields the
old format can't supply should be consistently `''`/`[]`, not omitted. LSA changelog accumulation
is optional (older archives may lack an LSA table) — skip gracefully.

## THE critical risk: text normalization at the seam
`content_hash` covers `text, type, instrument, part_title, subpart_title, section_title,
subsection_title, date, prescribed_by, reserved, end_marker, images` (see `store.HASH_FIELDS`;
`changes[]` and refs are deliberately excluded). If the archive parser flattens the SAME legal
text differently than `chunker.py` (whitespace, table→HTML conventions, `[IMAGE: id]` markers,
title period-stripping, paragraph label handling), the seam edition will hash everything as
"changed" and fabricate ~11k spurious version rows.

**Mandatory seam test before any real backfill:** pick an edition present in BOTH sources
(e.g. FAC 2023-02 or 2023-03 — GitHub has them; the archives should too), run it through the new
adapter into a COPY of the store, and require merge stats ≈ all `unchanged`/`extended_backward`
with near-zero `changed`. Iterate the parser until that holds. Reuse `extract_json.py` flattening
helpers wherever the archive format allows — archives from roughly 2019 on include DITA (possibly
an older dialect; try `chunker.py` first and patch), older editions are HTML (new parser, same
flattening conventions).

## Practical notes
- Effective dates: from the archive's own FAC labeling; pass explicitly per edition. Set
  `source_version` to the FAC label (e.g. "FAC 2019-01 …") — mirrors the ditamap-rev convention.
- Ingest order doesn't matter for correctness, but oldest-first gives the most readable reports.
- Test against store copies, never the live `store/` (its current rows carry verified LLM refs;
  historical rows are parser-only — archive backfill only adds/adjusts historical rows and must
  never touch current ones: any merge that reports `errata_*` or `changed` at the newest edition
  is a bug or a seam mismatch).
- After each backfilled edition: `python verify_store.py` (invariants) must stay clean.
- Environment quirk (Claude sandbox only): bash calls hard-timeout at 45s and background
  processes don't survive between calls — run long jobs in per-edition steps.

## Suggested first steps
1. Inventory the user's archive folder: formats per era (DITA zip? HTML? PDF?), FAC labels, dates.
2. Try `chunker.py` unmodified on the newest archived DITA edition; diff its rows against the
   GitHub-sourced rows for the same FAC (the seam test) to measure normalization drift.
3. Design per-era adapters from what actually breaks, not speculatively.
