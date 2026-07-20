# Plan: resolving RFO deviations in the v5 schema

**Driving use case (agreed):** agency-resolved retrieval — *"what text governs for
agency X on date D?"* — with a section-level baseline↔RFO crosswalk. Tenancy
interaction deliberately deferred (resolution is authority-parameterized; tenant
defaults can bind to it later).

## The problem in one sentence

v5 versions text along one axis (time, SCD-2 on `chunks`); the RFO adds an
**authority axis** — on a given date, up to three candidate texts exist per
address: baseline FAR (48 CFR, still in force), the FAR Council **model
deviation** text (its own issuance/UPDATE timeline), and per-agency instruments
that adopt / modify / decline it. "One current chunk per node" stops being true
unless something partitions by authority.

## Decision: where deviation data lives

Split by the nature of the artifact, not one mechanism for everything:

| artifact | nature | home |
|---|---|---|
| Model deviation text | a real regulation text: parts→sections, versioned (Issuance/UPDATE dates), renumbered vs FAR | **existing `nodes`/`chunks`** as a second **corpus** (`RFO`), through the unchanged merge engine — the AFARS pattern |
| Agency deviation PDFs | instruments *about* text: "we adopt the model text for parts 1–53", scoped, dated, superseded | **new tables** `authorities`, `deviation_instruments`, `deviation_coverage` |
| "What governs" | a derived fact | **resolution view/function** over coverage + the two corpora |
| Baseline↔RFO correspondence | graph data with confidence + evidence | **existing `edges`** with new cross-corpus `edge_type`s, built by the existing parser+LLM+review machinery |

Why not an `authority` column on `chunks` (the other option considered): the RFO
renumbers, merges and deletes sections (FAR part 38 folds into RFO part 8; part
52 clause numbering shifts), so RFO addresses are not FAR addresses — layering
RFO text onto FAR nodes would model one document inside another document's tree.
It also puts a second dimension inside SCD-2 identity: `merge_snapshot`, the
gap/overlap/current invariants, the audit queue and refs-verification would all
need per-authority variants. Separate corpus = zero change to any of that.
Retrieval filtering is equally cheap either way (partial indexes per corpus).

Why not chunk the ~800 agency PDFs: the only agreed runtime question is
agency-resolved retrieval, and for adopting agencies the operative text *is* the
model text — chunking adoption memos adds noise, not text. Agencies with real
modifications (GSA/CFTC supplements) become sparse overlay corpora in a later
phase, keyed to their instrument rows.

## Schema deltas

Small changes to existing tables:

- `nodes.corpus text NOT NULL DEFAULT 'FAR'` — explicit discriminator
  (`FAR` | `RFO` | later `RFO-GSA`-style overlays). Today corpus is implicit in
  the `FAR-` citation prefix; make it queryable. Backfill = one UPDATE.
- Denormalize `corpus` onto `chunks` and `chunk_representations` (or a generated
  column via join at ingest) so retrieval partial indexes
  (`WHERE corpus='RFO' AND is_current`) hit without joins.
- `nodes.part int` (generated from `far_address`) — resolution joins at part grain.
- `ingestion_runs.source_id` gains `acquisition-gov-rfo`.

New tables:

```sql
CREATE TABLE authorities (
  authority_id  text PRIMARY KEY,        -- 'DOD','GSA','NASA','FAR_COUNCIL',...
  name          text NOT NULL,
  parent_id     text REFERENCES authorities  -- room for components later
);

CREATE TABLE deviation_instruments (      -- one row per issued document
  instrument_id   uuid PRIMARY KEY,
  authority_id    text NOT NULL REFERENCES authorities,
  kind            varchar NOT NULL,       -- model_issuance | class_deviation | supplement | rescission
  title           text,
  file_ref        text,                   -- path into archive_rfo/ (raw PDF)
  url             text,
  issued_date     date,
  effective_from  date NOT NULL,
  effective_to    date,                   -- closed by supersession/rescission
  is_current      boolean NOT NULL,
  supersedes_id   uuid REFERENCES deviation_instruments,  -- UPDATE chains
  source_version  text,                   -- site's verbatim date_raw string
  extracted_text  text,                   -- searchable memo text (not RAG corpus)
  ingestion_run_id uuid,
  created_at      timestamptz
);

CREATE TABLE deviation_coverage (         -- what an instrument does, per unit
  coverage_id     uuid PRIMARY KEY,
  instrument_id   uuid NOT NULL REFERENCES deviation_instruments,
  authority_id    text NOT NULL,          -- denormalized
  part            int  NOT NULL,
  far_address     text,                   -- NULL = whole part; else carve-out grain
  adoption_mode   varchar NOT NULL,       -- adopts_model | adopts_model_modified
                                          --  | retains_far | rescinds
  pinned_version  text,                   -- NULL = rolling (follows RFO updates)
  effective_from  date NOT NULL,
  effective_to    date,
  is_current      boolean NOT NULL
);
```

Multi-part PDFs (`USAID_..._Parts-1-6-10-...pdf`, `DOC_..._Parts-1-53.pdf`) are
one instrument row, many coverage rows.

## Resolution

Per (authority, part, date): the in-force coverage row decides; absence of one
decides too (staggered rollout — a part with no issued model text or no adoption
yet resolves to baseline).

```sql
CREATE VIEW governing_text AS             -- conceptually; function form for as-of
SELECT a.authority_id, p.part,
       CASE c.adoption_mode
         WHEN 'adopts_model'          THEN 'RFO'
         WHEN 'adopts_model_modified' THEN 'RFO'   -- + instrument flag
         ELSE 'FAR' END AS corpus,
       c.instrument_id
FROM authorities a CROSS JOIN parts p
LEFT JOIN deviation_coverage c ON in_force(c, :as_of, a, p);
```

Retrieval wiring: compute the agency's part→corpus map first (≤53 rows, cached),
then ANN-search `chunk_representations` and post-filter candidates against the
map (over-fetch slightly), or maintain per-corpus partial HNSW indexes and issue
two filtered searches. Provenance rides along: every resolved answer can cite
`instrument_id` → the archived PDF (`file_ref`), satisfying audit needs later
without extra design.

`resolve(authority, address, as_of)` in code mirrors the view for point lookups
(export, spot checks), analogous to `Store.as_of()`.

## Crosswalk (baseline ↔ RFO, section level — agreed in scope)

Reuse `edges` end-to-end: cross-corpus rows `from_node = RFO-*`,
`to_node = FAR-*`, `edge_type ∈ {aligns_with, renumbered_from, absorbed_into,
no_counterpart}`, effective-dated like any edge. Build it with the machinery you
already trust: a deterministic pass proposes pairs (same address, then title
match, then text similarity across corpora); the LLM audits blind; disagreements
land on the existing review page; `mentions.evidence` carries the matched
headings/snippets. The practitioner-album **line-out documents** (in
`archive_rfo/albums/*/assets_docs/`) are the authoritative evidence source for
the hard cases (moves like 38→8, part 52 renumbering).

## Ingest phases

1. **Schema migration** — corpus columns + three tables + backfill (`corpus='FAR'`).
2. **RFO corpus adapter** — parse `archive_rfo/part-NN/` HTML (prefer the
   DITA-OT `FAR_Part_N.html` renders where the probe found them — the existing
   ditaot-era parser lineage; else the Drupal pages) → standard chunk rows →
   `merge_snapshot` per edition. Editions and `effective_from` come from
   `archive_metadata.json` (`effective_date` = UPDATE else issuance). The site
   shows only current text, so periodic re-runs of the downloader are how new
   RFO editions accrue.
3. **Instruments + coverage** — rows straight from `archive_metadata.json`
   `parts[].agency_deviations` (agency, url, file). `adoption_mode` needs the
   memo contents: default `adopts_model`, then an LLM classification pass over
   the PDFs (one-page memos mostly) with human review of low-confidence rows —
   same reconcile pattern as refs. Filename signals (`Supplement`, multi-part
   lists) pre-seed it.
4. **Crosswalk** — edges pass as above.
5. **Resolution + retrieval** — view/function, partial indexes, part→corpus
   map in the retrieval path. Optional later: `tenant_settings.default_authority`
   binding tenants to an agency (deferred by decision).
6. **Overlay corpora (deferred)** — chunk the few genuinely modified agencies'
   supplement texts as `RFO-<AGY>` corpora; resolution already has the
   `adopts_model_modified` hook.

## Invariants & verification (extends `verify_store.py`)

Per corpus: existing gap/overlap/current checks unchanged. New: every
`deviation_coverage` in-force range ⊆ its instrument's range; no two in-force
coverage rows for the same (authority, part, grain); every `adopts_model`
coverage row whose part lacks current RFO chunks must be flagged (adoption
before issuance = data bug); resolution spot checks in the
`--expect "DOD|2026-07-01|part 5|RFO"` style; crosswalk completeness metric
(every current RFO section has ≥1 alignment edge or an explicit
`no_counterpart`).

## Open questions (parked, non-blocking)

- Rolling vs pinned adoption: do any agencies pin to a model version rather
  than tracking UPDATEs? (`pinned_version` exists; classification pass will tell.)
- Rescission endgame: when formal rulemaking lands (FAC), instruments sunset —
  `effective_to` + a `rescinds` coverage row handles it; confirm no extra state
  needed.
- Tenancy binding (deliberately deferred): tenant⇒authority default vs
  per-query parameter; `content_acl` untouched either way (regulation text is
  public; ACL keeps gating tenant-private content).
