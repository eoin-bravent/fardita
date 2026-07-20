# FAR ingestion pipeline (chunk → LLM audit → human review → verified)

> **Fleet mode (all agencies):** `orchestrator.py` runs the whole archive→store pipeline per
> agency (download → survey → backfill → audit → optional LLM), `dashboard.py` serves a web
> GUI over it (`python dashboard.py` → http://localhost:8642), `nightly.py` (+
> `.github/workflows/nightly.yml`) keeps every store current from the GSA GitHub repos, and
> `data/agencies.json` is the registry of all ~34 regulations. Stores live in `stores/<AGENCY>/`,
> archives in `archive/<AGENCY>/` (downloader: `download_acquisition_archives.py`).

## What's in this folder

**The app** — `orchestrator.py` (pipeline engine), `dashboard.py` (web GUI), `nightly.py`
(GitHub updater), `download_acquisition_archives.py` (archive fetcher).
**Parsing & stores** — `store.py` (SCD-2 versioned store + merge engine), `chunker.py` +
`extract_json.py` (canon DITA chunker), `changelog.py` (LSA/rev change track),
`archive_adapter.py` (per-era archive parsers + seam/collapse/hints),
`backfill_archives.py` (oldest-first era driver).
**Verification** — `corpus_audit.py` (text-conservation proof), `verify_store.py`
(invariants + as-of checks), `store_coverage.py` / `verify_coverage.py` (git-history
completeness audits).
**LLM reference flow** — `pipeline.py` (run/audit/review/apply/update/replay/export),
`gemini_audit.py` / `vertex_audit.py` (backends), `reconcile.py`, `review.py`, `update.py`.
**Config & data** — `pipeline.config.json` (main config, stays at the package root);
`data/` = checked-in inputs: `agencies.json` (regulation registry) and the authoritative
edition dates (`archive_dates.json` for FAR, `afars_dates.json`, one file per regulation);
`.env.example`, `requirements-vertex.txt`.
**Docs** — this file; `demo_fields.py` (runnable field guide). Deeper references live in
`../docs/`: `ARCHIVE_ERAS.md` (parser eras + validation), `VERIFIED_FORMAT.md` (output schema).
Everything else is regenerable scratch and gitignored: `cache/` (era surveys `*_eras.json` +
structure hints, rebuilt by `survey` / `derive-hints`), `out/` (run scratch + LLM cache),
`store_*` backups. The data lives in `../stores/<AGENCY>/`.

Turns the FAR (a regulation published as DITA XML) into a verified map of its cross-references.
A parser and an LLM each find the references, a person resolves the disagreements in a browser,
and the result is a dataset where every reference is tagged with where it came from. Built for the
FAR, but the `regulation` setting makes it reusable for DFARS / AFARS / etc. Pure-Python, no SDKs
for the default path.

## Workflow
Everything lands in the **versioned store** (`store/` — every version of every chunk across FAC
editions; see "Versioned store" below). The flow:

1. **`update`** (or `replay` for history) — ingest the GSA repo's current edition into the store.
2. **`audit`** — LLM-audit ONLY the units whose current store rows lack verified refs (after a new
   FAC that's exactly its changed sections; the first ever run is the whole corpus). Add `--judge`
   to have the LLM pre-fill a recommendation per disagreement.
3. **`review`** — accept / reject / fix the flagged references in a browser; **Save & Apply**
   writes your decisions onto the store's current rows.
4. **`export`** — flat JSON of the rows in force on any date, when a downstream consumer needs one.

```
update: git pull ─► chunk ─► merge into versioned store (effective_from/to, current)
audit:  store's unverified units ─► parser refs + blind LLM audit ─► reconcile ─► [judge] ─► review page
review: you accept/reject/fix in the browser ─► Save & Apply ─► statuses onto store rows
```
(`apply --decisions <file>` is the manual alternative to Save & Apply. `run` remains as the
parser-only chunking tool: `run --no-llm` writes `out/<REG>_chunks.json` + manifest.)

## How it works (in plain language)
The pipeline finds every reference in the FAR **two independent ways and compares them**, then asks a
person to settle the disagreements.

1. **Chunk it.** The FAR ships as DITA (an XML format). The pipeline splits each section into pieces
   ("chunks") — by default the section plus each lettered paragraph — and pulls out the plain text.
   Tables are kept inline as HTML so they stay readable; each image becomes a short `[IMAGE: …]` marker.
2. **Find references two ways.**
   - A **parser** reads the markup directly. It's exact on the linked references (the `<xref>` tags) and
     on patterns it can recognize — ranges like "(a) through (f)", and statute citations like
     "41 U.S.C. 1303".
   - An **LLM** reads the same section *blind* (it never sees the parser's answers) and lists the
     references it finds. It's especially good at references written in plain prose with no link.
3. **Compare them (reconcile).** For each section the two lists are lined up: if both found a reference
   it's treated as **agreed** (kept automatically); if only one did, or they disagree, it's **flagged for
   a human**. Every reference is also checked against a master list of real FAR citations, so typos and
   made-up citations get caught.
4. **Review.** You open a web page and look only at the flagged ones — Accept / Reject / Fix each. An
   optional "judge" LLM pass can pre-fill a suggestion for each to speed this up.
5. **Done.** Every reference on the store's current rows is tagged with where it came from and
   whether a human approved it (`refs_verified_from` records the edition it was verified against).
   References to *other* documents (U.S.C., CFR, executive orders, etc.) are kept in a separate list.

Two things make it cheap to run again and again:
- **It remembers (caching).** The LLM's answer for each section is saved, so re-running only re-asks for
  sections whose text (or the prompt) changed — the first full run is the only expensive one.
- **The LLM never gets the answer key.** It works from the section text alone, which is exactly what makes
  its agreement with the parser meaningful (it can't just copy a provided list).

## Settings that matter most
(The full list is in the Configuration table further down; these are the ones you'll actually touch.)

- **Provider** — which LLM service to use: **USAi.gov** or **Google Vertex AI**. Set in `.env` (see
  *LLM backends* below). Both use `gemini-2.5-pro` by default.
- **`--judge`** — also run a second LLM pass that suggests an answer for each flagged item, so reviewing
  is faster. Optional; costs a bit more.
- **`--concurrency N`** — how many LLM calls run at once (default 8). Higher is faster but more likely to
  hit the provider's rate limit; set to 1 for one-at-a-time.
- **`--files 5.101 5.202 …`** — process only certain sections instead of the whole FAR. Great for a quick,
  cheap first run.
- **`bottom_level`** — how finely to split each section into chunks: `section` (one chunk for the whole
  section), `paragraph` (the default — the section plus each `(a)`, `(b)`, `(c)`), or deeper
  (`subparagraph`, …). Deeper means more, smaller chunks — and it does **not** change LLM cost, because the
  LLM always works one section at a time.
- **`pricing`** — the dollars-per-million-tokens used for the cost estimate; set it to your contract rate
  (or `0` to hide the dollar figure).

## Commands
Three subcommands — **`run`**, **`review`**, **`apply`** — plus shared override flags that work on all three.
```
# ---- run: chunk -> LLM audit -> reconcile -> review page ----
python pipeline.py run                                # full: chunk + LLM audit + reconcile + review page
python pipeline.py run --no-llm                       # PARSER ONLY: chunks + manifest (no API key; no audit/reconcile/review)
python pipeline.py run --files 5.101 5.203 6.302-2    # only these .dita files (names or paths) instead of the ditamap
python pipeline.py run --limit 50                     # audit only the first 50 units (cheap smoke test)
python pipeline.py run --judge                        # also run the LLM judge to pre-fill review recommendations
python pipeline.py run --auto-accept                  # hands-off: skip review, write verified.json (parser+LLM union)
python pipeline.py run --judge --auto-accept          # hands-off, applying the judge's verdict on each disagreement
python pipeline.py run --concurrency 16               # parallel LLM calls (default 8; 1 = sequential)
python pipeline.py run --no-changelog                 # skip parsing the LSA change table (on by default)
python pipeline.py run --mock-llm refs.json           # drive reconcile/review from a canned LLM-output file
python pipeline.py run --dump-payload 5.203           # print the exact prompt + raw .dita for one unit, then exit

# ---- review: serve the review page ----
python pipeline.py review                             # serve; "Save & Apply" writes verified.json, then stops the server
python pipeline.py review --port 9000                 # serve on a custom port (default 8765)

# ---- apply: apply exported decisions (manual path) ----
python pipeline.py apply --decisions decisions.json   # feed one decisions.json
python pipeline.py apply --decisions a.json b.json    # merge several; later files win per (unit, target, alternate)

# ---- shared override flags (any subcommand; highest precedence — see "Configuration precedence") ----
python pipeline.py run --regulation FAR               # regulation label + output filename prefix (default FAR)
python pipeline.py run --input-dir /path/to/dita      # DITA source folder
python pipeline.py run --output-dir out_vertex        # where outputs land (default out)
python pipeline.py run --bottom-level subparagraph    # deepest paragraph level to chunk
python pipeline.py run --provider vertex              # LLM backend: usai (default) or vertex
python pipeline.py run --model gemini-2.5-pro         # LLM model id
python pipeline.py run --reasoning   /  --no-reasoning   # toggle model reasoning
python pipeline.py run --thinking-budget 8000         # reasoning token budget
python pipeline.py run --no-judge                     # disable the judge / reconciliation pass
python pipeline.py run --config my.config.json        # use an alternate pipeline.config.json
```
- **`--no-llm`** is the parser-only switch — it stops after `chunks` + `manifest` (no API key, no
  review page). **`--files`** picks specific files; otherwise the file set comes from the **ditamap**
  (authoritative — see *Source & versioning*), falling back to scanning the whole `input_dir` folder.
  **`--judge`/`--no-judge`** toggles the optional reconciliation pass.
- **`--auto-accept`** skips the human review step and writes `verified.json` directly, for scheduled /
  hands-off runs. Policy: **with `--judge`** it applies the judge's verdict (accept/reject/manual) on
  each disagreement; **without**, it takes the **union** — every reference either tool found. Anything
  accepted this way is tagged `status: auto_accepted` (distinct from `human_approved`), so a later human
  pass can still audit or override it — the gate is skipped, not removed.
- **`--concurrency N`** runs N audit/judge calls in parallel (the audit is the slow stage — a full
  corpus is many hours sequential, ~N× faster threaded). Tune down if you hit provider rate limits
  (429s back off automatically). Subset (`--files`) runs build a **whole-corpus address map** (cached
  to `out/<REG>_addrmap.json`) so cross-file targets still validate.

## Source & versioning
- **Source.** `input_dir` points at GSA's authoritative DITA repo (`GSA/GSA-Acquisition-FAR`, the
  `dita/` folder), a sibling clone by default (`../../GSA-Acquisition-FAR/dita`). Override with
  `--input-dir` or `PIPELINE_INPUT_DIR` (it's a machine-local path).
- **File list from the ditamap.** When no `--files` are given, the file set is driven by `FAR.ditamap`
  (config `ditamap`) — the canonical, ordered list of real clauses — rather than a blind folder scan.
  This excludes backups/matrices/error-reports the scan would otherwise pick up. The map references
  **whole files only**, so the parser still does all intra-file paragraph decomposition. If the map is
  absent (or `ditamap: ""`), the run falls back to scanning `input_dir`. The manifest records which was
  used as `file_source` (`ditamap` / `folder` / `explicit`).
- **Change tracking.** `run` also records what each FAC changed — a section-level `<REG>_changelog.json`
  plus per-chunk `changes` spans. See **[Change tracking](#change-tracking-what-each-fac-changed)** below.
- **Version stamp.** **Every chunk** carries its own provenance — **`source_version`** (the FAR edition,
  read verbatim from the ditamap's `rev`, e.g. `FAC 2026-01 March 13, 2026`) and **`pipeline_version`**
  (this repo's git short SHA — explains output changes when the FAR itself didn't move). Stamping per
  chunk keeps `chunks.json` / `verified.json` a plain array (no envelope) and pre-shapes the rows for a
  versioned SQL load. The run timestamp **`chunked_at`** is recorded once at run level in
  `<REG>_manifest.json` (a per-row timestamp would be noise). `<REG>_token_usage.json` also keeps a
  run-level `version` block for the banner/cost report.

## Output record
`verified.json` (and `chunks.json`) is a plain **array of chunk records**. A record's identity is the
pair **`(citation, alternate)`** described by three independent axes: **`type`** (structural level, FAR
1.105-2: section / subsection / paragraph / …), **`instrument`** (functional: `clause` / `provision` /
`""` for an ordinary regulatory section), and **`alternate`** (variant: `""` for the base text, or an
arabic id `"1"`/`"2"`/… for a clause Alternate). A clause and its Alternates **share one `citation`** and
appear as **separate sibling records** distinguished by `alternate`. A full record:
```json
{
  "citation": "FAR-52.247-64", "regulation": "FAR",
  "type": "subsection", "instrument": "clause", "alternate": "",
  "part_title": "Solicitation Provisions and Contract Clauses", "subpart_title": "Text of Provisions and Clauses",
  "section_title": "", "subsection_title": "Preference for Privately Owned U.S.-Flag Commercial Vessels",
  "part": "52", "subpart": "2", "section": "47", "subsection": "64", "paragraph": "", "subparagraph": "",
  "source_version": "FAC 2026-01 March 13, 2026", "pipeline_version": "013d84a",
  "url": "https://www.acquisition.gov/far/52.247-64",
  "date": "Nov 2021", "prescribed_by": "47.507(a)", "reserved": false, "end_marker": "(End of clause)",
  "text": "(a) … the basic clause body …",
  "cross_references":   [ { "target": "52.247-64", "alternate": "1", "confidence": "explicit", "mentions": [ … ], "status": "corroborated" } ],
  "external_references":[ { "ref_type": "usc", "target": "usc:46/55305", "locator": "", "citation": "46 U.S.C. 55305", "mentions": [ … ], "status": "corroborated" } ],
  "images": [], "changes": []
}
```
Each `citation` is prefixed with the regulation (`FAR-5.101`, `FAR-6.302-2(a)`) so IDs stay unique across
regulation sets (FAR vs DFARS vs AFARS); a cross-reference `target` stays **bare** (`5.202(a)(2)`) since
it's within the same regulation. **Full field-by-field reference: [`VERIFIED_FORMAT.md`](../docs/VERIFIED_FORMAT.md).**

## Cross-references
Each chunk's `cross_references` is a list **grouped by `target`** (one entry per distinct cited
citation); every textual occurrence is kept as a mention:
```json
{ "target": "5.207(c)", "alternate": "", "confidence": "inferred",
  "mentions": [ {"kind": "inferred", "evidence": "…requirements of <xref href=\"5.207.dita#FAR_5_207\">5.207</xref>(c). The notice…"},
                {"kind": "inferred", "evidence": "…"} ] }
```
- `kind` (per mention) — `explicit` (a precise `<xref>` link) or `inferred` (resolved from a trailing
  qualifier, prose, or a range). `confidence` (per reference) = `explicit` if any mention is explicit,
  else `inferred`.
- `evidence` (per mention) shows the source sentence with the raw `<xref>` markup inline, windowed past
  the reference. (Same field name as the LLM/ledger evidence, for alignment.)
- `alternate` — `""` for a normal reference, or an arabic id when the reference is to a clause **Alternate**
  ("Alternate I of 52.204-30" or "… with Alternate I" → `target: "52.204-30", alternate: "1"`). The
  base clause and each Alternate are kept as **distinct** edges, keyed by `(target, alternate)`.
- **Ranges are expanded into atomic members** — `5.203(a) through (d)` becomes four references
  `5.203(a)`, `5.203(b)`, `5.203(c)`, `5.203(d)` (no `(a)-(d)` spans anywhere). All members share the
  range's `evidence`. The enumerator handles letters / digits / romans / numeric subsection dashes and
  every separator (`-`, `–`, `to`, `through`, repeated citation); a genuinely ambiguous range (e.g.
  `(i)-(v)`, letter vs. roman) is left for the LLM + human rather than guessed.
- References to **other government documents** (U.S.C., CFR, E.O., …) are captured separately — see
  **External references** below. Bare web URLs / emails are excluded.

## External references (other government documents)
Each unit also carries an `external_references` list — edges to documents *outside* this regulation
(for the eventual Graph-RAG, these are nodes you may ingest later). Handled the **same way as internal
refs**: the parser catches the rigid formats, the LLM catches the long tail, and they reconcile into the
same statuses (corroborated / parser_explicit / llm_only) and human review (a **scope filter** splits
internal vs external on the review page).

```json
{ "target": "usc:41/1303", "ref_type": "usc", "citation": "41 U.S.C. 1303(a)(4)",
  "locator": "(a)(4)", "division_levels": ["41","1303","a","4"],
  "mentions": [ {"kind":"explicit","evidence":"…as defined in 41 U.S.C. 1303(a)(4)…"} ], "status": "parser_only" }
```
- **`target`** is a canonical **node id** — `usc:<title>/<sec>`, `cfr:<title>/<part.sec>`, `eo:<num>`,
  `publ:<cong>-<num>`, `omb:<series>-<num>`, `form:SF-33` (forms), or the URL itself (`url`). The node is
  the doc/**section**; the precise sub-part rides on the edge as **`locator`**, so many references to one
  document collapse to one node.
- **`ref_type`** ∈ `usc | cfr | eo | public_law | omb` (statutory citations) · `form` (Standard / Optional
  / DD forms) · `url` (any other tagged external link, e.g. a NIST or agency page).
- **`href`** — a resolvable link when the source provided one (gov form pages, `uscode.house.gov`, …);
  also attached to the statutory reference a tagged link points to.
- **`division_levels`** is the full parse (title, section, subsections…), mirroring the DITA decomposition.
- **How they're found:** statutory citations come from **regex over the prose** (high-precision formats
  only — named statutes/"the X Act" are deliberately excluded, they were too noisy). **Forms and URLs come
  from tagged `<xref>` links** in the source, so they're reliable (the author explicitly linked them).
  The **LLM** also reports statutory externals (restricted to those formats; anything it can't format is
  dropped at reconcile). LLM-only externals get human review; the LLM judge doesn't run on externals.
- **Editing**: on the review page, an external row's **Manual** option gives two fields — **Document**
  (the node, e.g. `Small Business Act` or `41 U.S.C. 1303`) and **Section** (the locator, e.g. `8(a)`) —
  so you correct the document and the section independently; `apply` rebuilds the canonical edge.

## Change tracking (what each FAC changed)
The FAR is republished as **Federal Acquisition Circulars (FACs)**. Each `run` captures what the current
FAC changed, at **two granularities** — produced **deterministically** (no LLM / reconcile): the change
data is *explicit markup*, so there's exactly one correct reading. (Contrast the cross-reference work,
where the two-finder + judge machinery exists precisely because finding refs in prose is ambiguous.)

**1. Section-level — `<REG>_changelog.json` (the "what changed" product).**
Parsed from **`LSATable.dita`**, the FAR's own **List of Sections Affected** — a clean DITA table the
publisher ships with each FAC. One entry per amended section:
```json
{ "section": "25.1101", "citation": "FAR-25.1101", "paragraphs": ["(b)(1)(iii)","(b)(2)(iii)"],
  "description": "Amend section 25.1101 by— a. Removing from paragraph (b)(1)(iii) “$102,280” and adding “$105,767” in its place; and b. …",
  "case_number": "FAR Case 2025-007",
  "federal_register_url": "https://www.federalregister.gov/d/2026-04912/p-amd-6",
  "source_version": "FAC 2026-01 March 13, 2026", "pipeline_version": "<sha>" }
```
This is the **change-first** view ("what changed across the FAR this edition?") and the input for the
FAC tools (amendatory-instruction generation, change summaries). Skip it with `--no-changelog`.

**2. Span-level — the `changes` list on each chunk (the "what changed *here*" detail).**
Inside the amended clause files, the changed words are wrapped in `<ph rev="FAC …">` track-change tags,
with `<?FM MARKER [CaseNumber]?>` / `<?FM MARKER [Why]?>` processing instructions next to them. Each
chunk carries the spans that fall inside it (`[]` when unchanged):
```json
"changes": [
  { "text": "$105,767,", "fac": "FAC 2026-01 March 13, 2026", "case_number": "FAR Case 2025-007",
    "why": "b. Removing from paragraph (b)(2)(iii) “$102,280” and adding “$105,767” in its place." }
]
```
- `text` / `fac` come from the chunker's normal parse (the exact changed words + the `rev` attribute).
- `case_number` / `why` come from those inline markers — but **ElementTree silently drops processing
  instructions**, so they're recovered with a **separate PI-preserving parse** (`insert_pis`, which
  honors the PI's `?>` boundary per the XML spec) and aligned to the chunker's PI-free spans **by
  document order**. The markers are *not* read from the main parse because `insert_pis` would fold their
  text into the flattened chunk `text` — so the two parses are kept separate (verified: chunk `text` is
  byte-identical with and without).
- A change is listed on **every containing chunk** — the section chunk *and* the specific paragraph —
  the same inheritance as `text`/refs. So whichever level you retrieve, you see the relevant change.

**How the two compare / complement:**
| | `changelog.json` (section) | chunk `changes` (span) |
|---|---|---|
| source | `LSATable.dita` (LSA table) | `rev` attribute + `[CaseNumber]`/`[Why]` PIs in the clause |
| grain | one entry per amended **section** | one item per changed **span**, on every containing chunk |
| description | **complete** amendatory instruction | the span's **fragment** of that instruction (the `why`) |
| extras | `federal_register_url` (section-level) | exact changed `text` + position (for redline/highlight) |
| serves | change-first ("what changed this FAC") | content-first ("what changed in *this* clause") |

They're complementary, not duplicates — and they cross-check: the per-span `why` fragments are
*contained in* the section's full `description` (the parts add up to the whole). `run` asserts exactly
that at build time (`whys in LSA description M/M [OK]`), so any truncation or misalignment would alarm.
Some spans have an **empty `why`** (e.g. a table revision the source describes once but `rev`-marks per
cell) — expected, not a failure.

**Scope.** A single FAR export carries only the **current** FAC's `rev` marks (older ones drop out each
release), so this is "changed in *this* edition." Accumulating history across FACs is the job of the
versioned store — see the next section.

## Versioned store (`update` / `replay` — history across FACs, no LLM)

The versioned store (`store/`, see `store.py`) retains **every version of every chunk** across FAC
editions, SCD-Type-2 style. Each row is the standard chunk record plus:

| field | meaning |
|---|---|
| `effective_from` / `effective_to` | **legal** effective dates, half-open `[from, to)`; `effective_to: null` = still in force. In force on date X ⇔ `effective_from <= X < effective_to`. Consecutive versions share the boundary date (no gaps, no −1 day). |
| `current` | `true` iff `effective_to` is null (denormalized for cheap filtering) |
| `content_hash` | change detector over text + structure + titles + clause meta; deliberately **excludes** `changes[]` (rev marks drop out each FAC) and cross/external refs (verified statuses survive unchanged chunks untouched) |
| `ingested_at`, `source`, `source_commit` | observation provenance (`gsa-github` \| `acquisition-gov-archive` \| `manual`; commit SHA) |
| `last_seen_version` / `last_seen_date` | most recent ingested edition confirming this exact content |

Identity is `(citation, alternate)`; a version is `(citation, alternate, effective_from)`. A chunk
untouched by a FAC keeps its single row spanning editions — the store grows only by deltas.

**One merge operation, three uses.** `Store.merge_snapshot()` reconciles a complete edition snapshot
at effective date D into the store, whether D lands **after** (daily update), **before** (archive
backfill — identical text extends a row's floor backward; differing text inserts a closed row), or
**between** known editions. It's idempotent and order-independent. Mid-FAC **corrections (errata)** —
content changed but same edition — are replaced **in place** (the legal timeline doesn't move); the
superseded row is appended to `store/<REG>_errata.json` so nothing observed is lost, and the row's
`refs_verified_from` stamp is **cleared** (the replacement carries parser-only refs for changed text,
so the unit re-queues for the next `audit`; the errata log keeps the superseded row with its verified
refs intact). Per-era **adapters** (the chunker is the GitHub-DITA one; `archive_adapter.py` provides
the acquisition.gov-archive ones) produce standard chunk rows; the merge engine alone owns time.

```bash
# daily/scheduled: ingest the GSA clone's current state (new FAC -> new versions; same FAC -> errata)
python pipeline.py update [--repo R] [--effective-date YYYY-MM-DD] [--force]

# one-time / backfill: replay historical editions (settled commit per FAC) through the same path
python pipeline.py replay --repo ../../GSA-Acquisition-FAR --since 2025-04 [--errata-check]

# LLM audit driven by the store: only units whose current rows lack verified refs
python pipeline.py audit [--judge] [--files 22.1503 …] [--auto-accept]

# human review (unchanged UI); Save & Apply now writes decisions to the STORE
python pipeline.py review          # or: python pipeline.py apply --decisions d.json

# flat JSON of the rows in force on any date (replaces verified.json)
python pipeline.py export [--as-of 2024-01-15] [--out path.json]

# invariants + summary + as-of spot checks
python verify_store.py --expect "FAR-22.1503(b)|2026-03-12|\$102,280" \
                       --expect "FAR-22.1503(b)|2026-03-13|\$105,767"
```

**The store is the single source of truth — the LLM flow reads from and writes to it.**
`audit` asserts the input tree matches the store's current rows (same edition per `state.json`,
same content hashes — chunk-level, so GSA file quirks can't hide drift), then audits ONLY the
units whose current rows lack `refs_verified_from` (or an explicit `--files` list). That queue is
self-maintaining: a new FAC's `update` creates fresh version rows, which are born unverified,
which puts exactly those units on the next `audit`'s queue — everything else is skipped (the
`out/llm_cache/` gives a second, model-keyed skip layer). `apply` — whether from the review page,
`--decisions`, or `--auto-accept` — tags statuses directly on the audited units' current store
rows and stamps `refs_verified_from`; untouched units and historical versions are never modified.
Because `content_hash` excludes references, verified refs ride through every future merge on
unchanged chunks. Verification attaches to a *version*: a changed chunk's new row starts
parser-only even when its predecessor was verified (text changes can add or remove references),
and historical rows keep the refs they were verified with. `<REG>_verified.json` is retired;
`export` produces flat snapshots for downstream consumers — for any date the store covers, not
just the current edition. (The one-time migration of the pre-store `verified.json` onto store rows
was done with a `store-apply` bridge, since removed.)

`update` detects new-edition vs errata from the ditamap `rev`'s FAC id, parses the **legal effective
date from the rev** ("FAC 2026-01 March 13, 2026" → `2026-03-13`; `ingested_at` records observation
separately), chunks the full file set (`content_hash` makes unchanged chunks no-ops), merges, appends
the FAC's LSA entries to **`store/<REG>_changelog.json`** (accumulated, keyed by `source_version` —
the persistent "what did each FAC touch" index), and writes a two-way **LSA discrepancy report** to
`store/reports/`. `replay` walks first-parent history and replays each FAC's **settled** (last) commit
— interim rev text ("August XX") and mid-FAC churn fold in; `--errata-check` also pre-ingests an early
commit of the final FAC so the settled pass exercises the errata path. `store/state.json` remembers the
last processed commit, so `update` after `replay` continues seamlessly.

Store files: `store/<REG>_store.json` (all version rows + edition registry), `<REG>_changelog.json`,
`<REG>_errata.json`, `state.json`, `reports/`. Query in code: `Store.as_of(date, citation=…)`,
`Store.current_rows()`, `Store.verify()` (gap/overlap/current-flag invariants).

**Validated against the full usable GitHub history — FAC 2023-02 (March 2023) → FAC 2026-01 — 18
editions, 14,245 rows, 1,696 multi-version chains.** Replay ran forward (2025-04 → 2026-01, with
`--errata-check`) then BACKWARD (2023-02 → 2025-03 backfilled into the existing store), exercising
every merge path: bootstrap, new-edition, errata (the FAC 2026-01 pre-publication correction to
25.402 is in the errata log), extend-backward (~10k rows), mid-chain splits, deletions, reopenings.
Invariants clean; re-merge idempotent (0 events); as-of queries return $102,280 through 2026-03-12
and $105,767 from 2026-03-13 for FAR-22.1503(b). Earlier than FAC 2023-02 the ditamap carries no
`rev`, so pre-2023 history comes from the acquisition.gov archives (see **acquisition.gov archive
adapters** below). Two interim
FACs (2024-02, 2024-04) left no parseable settled rev; their changes are captured but attributed to
the next ingested edition's date (ingestable individually later via `update --effective-date`).

### acquisition.gov archive adapters (`archive_adapter.py` — history before the GitHub repo)

Extends the store earlier than the GitHub history using the downloaded acquisition.gov
archives (`../archive_far/`). The archives span several HTML-generator eras; **`archive_adapter.py
survey`** classifies every folder and **[`ARCHIVE_ERAS.md`](../docs/ARCHIVE_ERAS.md)** documents each.
**All four HTML eras have parsers**: **ditaot** (2021-07 … 2025-06, DITA-OT XHTML, `FAR_Part_N.html`
per part), **webworks-2005** (168 "FAC 2005-xx" folders, ~2005–2018, `Subpart 5_1.html` per subpart),
**webworks-2001** (42 folders, ~2001–2005, `Subpart_5_1.html`, `Heading1`/`Body`/`Indented` +
`<dl>/<dt>` markup), and **legacy** (52 folders, 1995–2002, class-less per-part plain HTML) — every
parseable archive edition, FAC 1990-34 (1995) onward, can be chunked, seam-checked and backfilled.
`chunk` auto-detects the era (override with `--era`); `backfill_archives.py --eras <era>[,<era>…]`
drives any of them oldest-first (261 editions across the three pre-2019 eras).

```bash
# once: per-unit structure hints from the store itself (see below)
python archive_adapter.py derive-hints --store-dir store --date 2023-03-16

python archive_adapter.py survey ../archive_far                       # classify all folders -> archive_eras.json
python archive_adapter.py meta  ../archive_far/2023-01_HTML_Files     # FAC label + authoritative date
python archive_adapter.py chunk ../archive_far/2023-01_HTML_Files \
       -o rows.json --hints-store-dir store                           # era auto-detected; hints as-of edition date
python archive_adapter.py seam  rows.json --store-dir <COPY> --date 2023-03-16   # HASH_FIELDS diff
python archive_adapter.py ingest rows.json --store-dir <COPY> \
       --edition-dir ../archive_far/2023-01_HTML_Files [--collapse-cosmetic]   # date/version auto-filled
```

**Effective dates** for every edition (all eras) come from `archive_dates.json` — the authoritative
published dates scraped from `acquisition.gov/archives`, keyed by folder name. `ingest --edition-dir`
fills `--date`/`--source-version`/`--commit` from it automatically; no manual date entry.

The **webworks-2005** parser reads structural depth from the FrameMaker `pBody`/`pIndentedN`
classes (so it's largely self-sufficient; hints only reject a few mis-rendered nested items).
Because it's a **different source lineage** than the DITA store, the same legal text renders with
different typography (dashes, `U.S.C.` spacing, quotes), so a naive ingest would create a batch of
rendering-only "versions" at the single 2005↔2021 seam. **`ingest --collapse-cosmetic`** solves this:
it snaps chunks that differ from the store only cosmetically back onto the existing row (extend
backward, no new version) and ingests real differences verbatim, writing an audit log of every
collapse. Conservative by default (a differing clause `date`/`instrument`/etc. stays a real
version). Validation and the seam mechanics are in [`ARCHIVE_ERAS.md`](../docs/ARCHIVE_ERAS.md).

**Why hints.** The archive HTML is FrameMaker output: its `ListLn` classes encode VISUAL
depth, while the chunker's line/row structure follows the DITA's STRUCTURAL nesting — and
the FM→DITA conversion was irregular (ol-inside-p definitions, Runin items, li lists
promoted/demoted a level, labels as literal text, spacing quirks like `(ii)Sold`).  Those
distinctions are unrecoverable from the HTML alone, so `derive-hints` extracts each unit's
line/row skeleton (ordered line heads, per-line row labels+heads, per-line nospace joints,
end-marker string, breadcrumb titles) from the store's `as_of(date)` view; the parser then
reconstructs lines by ordered matching against them. Units absent from the hints (sections
dead before the store's history begins) fall back to class-based rules.

**Seam test (mandatory before any real backfill).** FAC 2023-02 is in both sources:
archive-parsed rows vs `store.as_of(2023-03-16)` agree on **all 11,629 identities with
11,627 hash-identical** — the 2 residuals are `52.225-2`/(a), whose archive HTML has
invalid nested `<p>` markup that no parser can render to byte parity. Cross-edition checks
(hints as-of each edition): FAC 2023-06 ≈99.3%, FAC 2025-06 ≈99.7% (late residuals are
real whitespace drift in the newer GitHub DITA, irrelevant to pre-2023 backfill).

**Backfilled (validated on a store copy):** 9 pre-store editions — FAC 2022-01
(2021-12-06), 2022-03 (2022-01-01), 2022-02 (2022-01-14), 2022-04 (2022-01-30), 2022-06
(2022-05-26), 2022-07 (2022-08-10), 2022-08 (both its 2022-09-23 and 2022-10-28 states),
2023-01 (2022-12-30) — merged oldest-first: **zero `changed`/`errata` at the 2023-02 seam**
(the first ingest was 10,862 `extended_backward` + 589 closed historical versions + 3 new),
per-FAC deltas of 2–22 sections thereafter, invariants clean, re-merge idempotent, current
rows (and their verified refs) untouched. Notes: archive folder names lie (the `2021-07`
folder is internally FAC 2022-01 — trust `LSATable.html`, never names); FAC 2019-01 …
2021-06 are absent from the downloaded archives; LSA changelog accumulation from
`LSATable.html` is not wired yet (skipped gracefully).

**Completeness proof — `store_coverage.py`.** Two independent audits against git itself:

```bash
python store_coverage.py --repo ../../GSA-Acquisition-FAR             # file-level audit
python store_coverage.py --repo ../../GSA-Acquisition-FAR --snapshot  # gold standard
```

The **file-level audit** takes `git diff --name-only` between each consecutive pair of settled
commits and requires every changed section file to be accounted for: store version events at that
edition (attributed by the citations the file actually produces — GSA filenames lie), proven
markup-only (both blob versions re-chunked and content-hash-identical: rev marks dropping out,
whitespace), an orphan (not referenced by the ditamap — GSA leaves stubs), or a rename (content
moved between files, identity unchanged). The **snapshot audit** is the definitive one, immune to
GSA's file-naming games (misnamed files like `42.200.dita` holding section 40.201, duplicate
sources during the Part 40 renumbering): for every ingested edition it re-chunks the full published
tree at the settled commit and requires the store's `as_of(effective_date)` view to be identical —
same identities, same content hashes. **All 18 editions: 0 missing, 0 extra, 0 hash mismatches.**

## Configuration — `pipeline.config.json`
| key | meaning | default |
|-----|---------|---------|
| `regulation` | label stamped on every row; names outputs | `FAR` |
| `input_dir` | folder of `.dita` files (GSA repo's `dita/`; relative paths resolve against the pipeline dir) | `../../GSA-Acquisition-FAR/dita` |
| `ditamap` | map file in `input_dir` giving the authoritative file list + version stamp; `""` forces a folder scan | `FAR.ditamap` |
| `bottom_level` | deepest chunk level: `section`/`subsection` (unit only) · `paragraph` · `subparagraph` · `subunit-depth-1…4` | `paragraph` |
| `url_template` | source link, `{num}` filled with the citation | acquisition.gov/far/{num} |
| `output_dir` | where outputs land | `out` |
| `store_dir` | the versioned chunk store (see "Versioned store") | `store` |
| `gemini.model` | model id (you set the highest available) | `gemini-2.5-pro` |
| `gemini.reasoning` | thinking on/off (on recommended for ambiguous refs) | `true` |
| `gemini.thinking_budget` | token budget (`-1` = dynamic) | `-1` |
| `gemini.judge` | optional 2nd LLM pass that pre-fills review recommendations | `false` |
| `concurrency` | parallel LLM calls per run (1 = sequential) | `8` |
| `pricing.input_per_1m` / `pricing.output_per_1m` | $ per 1M tokens for the cost estimate (0 disables) | `1.25` / `10.0` |

Chunking goes from the file's own unit (section/subsection) **down to** `bottom_level`; parents
keep full text (overlap). Decomposition fields run `part … <bottom_level>`, bare, empty below
the chunk's level. Below `subparagraph` we use `subunit-depth-N` rather than inventing names.

## Configuration precedence
`CLI flags  >  .env / environment  >  pipeline.config.json  >  built-in defaults`

- **`.env`** (copy from `.env.example`, gitignored) holds the secret + common defaults:
  `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_REASONING`, `GEMINI_THINKING_BUDGET`, `GEMINI_JUDGE`, and
  optional `PIPELINE_REGULATION` / `PIPELINE_INPUT_DIR` / `PIPELINE_BOTTOM_LEVEL` / `PIPELINE_OUTPUT_DIR`.
- **CLI override flags** beat everything — `--regulation --input-dir --output-dir --bottom-level
  --provider --model --reasoning/--no-reasoning --thinking-budget --judge/--no-judge --concurrency
  --config` (work on `run`/`review`/`apply`; one-line descriptions in **[Commands](#commands)**).
- Real environment variables beat `.env`; `.env` beats the JSON; JSON beats defaults.

## LLM setup
- Put your **government key** in `.env` as `GEMINI_API_KEY=…` (or export it) — read from env only,
  never written to config/logs.
- **What each call sends:** the system prompt + the **entire raw `.dita` file** of that unit (not the
  flattened text), so the model sees the real `<xref href=…>` markup. Inspect the exact bytes with
  `python pipeline.py run --dump-payload <citation>`.
- **What the model returns:** one `target` per reference — **ranges are expanded into one reference
  per member** (`5.203(a) through (d)` → four targets, *not* a span). `evidence` is the **complete
  source sentence(s) quoted verbatim**, with the citation that triggers the reference wrapped in
  `« »` guillemets (so the review page highlights it). The prompt is attachment-aware — a
  parenthetical after a link *usually* narrows it but **not always** (e.g. `'the authority of 5.202
  and (a)(2) of this section'` → `5.202` **and** this section's `(a)(2)`). It also carries a citation
  across a **paragraph list** (`5.202(a)(1), (a)(4) through (a)(9)` → all under 5.202, not this
  section) and **excludes self-references** (a unit citing itself / "this section"). Its
  **highest-value job is prose references with no `<xref>` tag** (e.g. `'as required by 5.207'`) — the
  deterministic parser already catches every tagged link, so the prompt tells the model those untagged
  refs are exactly what it misses and to scan for them.
- **Self-references are also dropped in code** (`reconcile`), so a unit→itself edge never reaches the
  ledger regardless of what the model returns; a *different* paragraph of the same section is kept.
- **Optional LLM judge (`gemini.judge` / `--judge`):** a second pass, once per `.dita` file, that
  sees **only that file** — its raw source + that file's **disagreements** (parser-inferred and
  llm-only atomic targets, with the finder's evidence) — and recommends `accept` / `reject` / `manual`
  + a one-line rationale for each. It **pre-fills** the review page's selection (you bulk-accept or
  override) — it never finalizes, so the human gate stays. Off by default; moderate extra tokens.
- Temperature 0, structured JSON output, reasoning/thinking on. Responses are **cached** per unit by
  (provider, model, prompt version, text hash) in `out/llm_cache/`, so re-runs are cheap and prior
  human decisions are never lost.
- **Rate limits matter**: ~2,964 units = one call each. `--concurrency` parallelizes them; backoff on
  429/5xx is built in. Tune concurrency to your tier.

## Performance & token usage
- **Concurrency**: audit + judge calls run in a thread pool (`--concurrency` / `concurrency` config /
  `LLM_CONCURRENCY`, default 8). Cache hits cost nothing, so reruns of unchanged units are free.
- **Token usage** is captured per call (prompt / output / **thinking** / total, by stage) — counting
  only real API calls, not cache hits — and surfaced three ways: the **console** summary, a banner on
  the **review page**, and `out/<REG>_token_usage.json` (with a per-unit breakdown). USAi reports usage
  only if its gateway populates the `usage` field; Vertex always does (incl. gemini-2.5 thinking tokens).
- **Timing**: per-stage (chunk / audit / judge / reconcile) + total wall-clock, in the console, the
  banner, and `token_usage.json`.
- **Cost estimate**: tokens × the `pricing` rates (in console / banner / `token_usage.json`). Rates live
  in `pipeline.config.json` → `pricing` (`input_per_1m`, `output_per_1m`, `currency`) — defaulted to the
  **public Gemini 2.5 Pro** rate ($1.25/$10 per 1M; thinking billed at the output rate). Set them to
  your contract rate, or `0` to hide the dollar figure. (A real 8-unit audit+judge run ≈ $0.38.)

## LLM backends (USAi.gov · Vertex AI)
Two interchangeable backends behind the same interface — pick per run; everything downstream
(reconcile / review / apply) is identical.

| provider | module | transport | auth | deps |
|----------|--------|-----------|------|------|
| `usai` (default) | `gemini_audit.py` | USAi.gov OpenAI-compatible REST (`urllib`) | `GEMINI_API_KEY`/`USAI_API_KEY` + `USAI_BASE_URL` | stdlib only |
| `vertex` | `vertex_audit.py` | Google Vertex AI (`google-genai` SDK) | ADC via `GOOGLE_APPLICATION_CREDENTIALS` | `pip install -r requirements-vertex.txt` |

Select with `--provider vertex`, `LLM_PROVIDER=vertex`, or `"provider": "vertex"` in the config.
Both call the same model (`gemini.model`, default `gemini-2.5-pro`) at temperature 0 with the
same prompts/schemas; the Vertex path uses Gemini's native JSON mode + `thinking_config`.
Vertex responses cache into a **separate** `out/llm_cache_vertex/` dir, so the two backends
never clobber each other's cached audits.

**Running the Vertex backend (e.g. on a GSA machine):**
1. `pip install -r requirements-vertex.txt`  (Python 3.9–3.13 recommended for the SDK)
2. Put the service-account JSON key on disk (git-ignored) and set
   `GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json` — the same env var the Java
   sample uses. Project/location default to the GSA values (`prj-t-ogp-acqsplcy-mvcai` /
   `us-central1`); override via `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` if needed.
3. `python pipeline.py run --provider vertex`  (add `--limit 5` for a cheap smoke test).

To keep both backends' full outputs side by side, give each its own `--output-dir`
(e.g. `--output-dir out_vertex`); otherwise a run overwrites the previous run's output files.

## Reconcile policy — atomic master list
Because every reference is atomic, reconciliation is a symmetric set comparison per unit. Each atomic
target lands in a **master list (ledger)** tagged by who found it:
- **corroborated** (parser AND LLM) → auto-accept.
- **parser_explicit** (parser via `<xref>`, LLM didn't) → auto-accept; markup is authoritative.
- **parser_inferred** (parser via prose/range, LLM didn't) → **review** (lower-confidence guess).
- **llm_only** (LLM found, parser missed) → **review** (the high-value catch).

`needs_review = parser_inferred | llm_only` — only these go to the human queue and the LLM judge
(corroborated/explicit are shown read-by-default but stay editable). **`--auto-accept`** resolves this
queue without a human: with `--judge` it follows the judge's per-item verdict; otherwise it accepts the
union (keep every parser ref, accept every `llm_only`) — those refs land in `verified.json` as
`auto_accepted`. A former "conflict" (LLM `5.202`
vs parser `5.202(a)(2)`) is simply two atomic rows: `llm_only 5.202` + `parser_* 5.202(a)(2)`.
Every LLM target is validated
against the FAR address map (grammar + existence); non-citations (U.S.C., URLs, hallucinations)
are dropped.

## Review page (`out/<REG>_review.html`)
Self-contained, no server. Shows the **full master list** — one row per atomic target with its status
badge, three evidence columns (**Parser** with inline `<xref>` highlighted, **LLM** with the `« »`
span highlighted, **Judge** recommendation + rationale), and a link to the unit on **acquisition.gov**.
Rows are **grouped by unit** and shown in **natural FAR order** (`(a)(1) < (a)(4) < (a)(11)`; romans
by value), with a top **banner** summarizing the run (provider/model, status counts, tokens, timing,
cost, cache hits). Status labels are plain-English — **Both agree** (corroborated), **LLM only (parser
missed)**, **Parser guess (LLM missed)**, **Tagged link (LLM missed)**, **Manually added** — with a
hover tooltip on each spelling out what it means. **Every row is editable** with a uniform choice: **Accept /
Reject / Manual**. When a judge ran, its recommended option is tagged **`judge ✓`** (click it to
re-select). Each unit also has an **"Add reference(s)"** box for refs neither tool found. Both the
Manual and Add boxes accept **comma lists *and* ranges**, expanded client-side into atomic citations
(`5.203(a)-(c)` → three) — same rules as the parser. **Status filters** toggle which rows show (by
default the disagreements + added; tick **Corroborated** / **Parser-only (explicit)** to inspect
agreements; **hide decided** to focus). The **Show** filter bar stays pinned at the top while you scroll
(the token/cost banner scrolls away).

**Pagination.** A full-corpus ledger is 10k+ rows, so the page is **paginated** — it renders one page
at a time (≤12 units, broken at FAR part boundaries, labeled by citation range) with a sticky
**◀ Prev / Next ▶** bar, a **Jump** dropdown, and a live per-page progress count. Only the current
page's rows are built (in time-boxed batches), so it stays responsive at any scale. All decisions live
in one in-memory map keyed by row, so they **persist across pages and reloads** and a single
**Save & Apply** writes them all — paging is purely a view over the one decision set; your place is remembered.

**Two ways to finish:**
- **Served (one click):** `python pipeline.py review` serves the page on `localhost` and opens it. Click
  **Save & Apply ▶** — your decisions are written to `out/<REG>_decisions.json` and `apply` runs
  immediately, producing `out/<REG>_verified.json`. No Downloads, no second command.
- **Manual:** click **Export decisions** to download `decisions.json`, then run
  `python pipeline.py apply --decisions <path>/decisions.json`.

**Reviewing over multiple sittings:**
- Your selections **auto-save** to the browser (`localStorage`), so reloading the page restores them.
- **Import ▲** loads a prior `decisions.json` back into the page (restores every selection) so you can
  resume, tweak, and re-export the complete set.
- `apply` accepts **multiple** `decisions.json` files and merges them, **later files overriding earlier**
  per `(unit, target)` — so incremental passes combine into one final `*_verified.json`:
  ```
  python pipeline.py apply --decisions pass1.json pass2.json
  ```

## Outputs (in `output_dir`)
| file | what |
|------|------|
| `<REG>_chunks.json` | the chunks (pristine, parser-only) — each row carries `source_version` + `pipeline_version`, `cross_references` (internal), `external_references`, `images` (deduped id list; inline `[IMAGE: id]` token in `text`), and `changes` (this FAC's `rev`-marked spans in the chunk; `[]` if none). Tables are inlined as HTML directly in `text`. |
| `<REG>_manifest.json` | every file **seen**, **processed**, and **skipped** (with reasons); plus `file_source` (ditamap/folder/explicit) and `chunked_at` (the run timestamp — per-chunk versions live on the chunks) |
| `<REG>_changelog.json` | **change track**: this FAC's List of Sections Affected, parsed from `LSATable.dita` — one entry per amended section (`section`, `citation`, `paragraphs`, plain-language `description`, `case_number`, `federal_register_url`), each stamped with `source_version` + `pipeline_version`. Written by `run` unless `--no-changelog`. |
| `<REG>_ledger.json` | the per-unit master list: every atomic target tagged `status` (corroborated / parser_explicit / parser_inferred / llm_only), with parser/llm/judge evidence — drives the review page and `apply` |
| `<REG>_token_usage.json` | per-run token usage (prompt/thinking/output/total by stage, per-unit), timing, status counts, cache hits |
| `<REG>_addrmap.json` | cached whole-corpus address map (so `--files` subset runs validate cross-file targets) |
| `<REG>_review.html` | the review page |
| `<REG>_verified.json` | after `apply`: the final dataset — a plain array of chunks (each with `source_version` + `pipeline_version`) + verified refs (`cross_references` + `external_references`), every ref tagged with a flat `status` (`corroborated`/`parser_only`/`human_approved`/`auto_accepted`). **Field-by-field structure: [`VERIFIED_FORMAT.md`](../docs/VERIFIED_FORMAT.md).** |
| `llm_cache/` | cached raw LLM audit + judge responses |

The reviewer's **`decisions.json`** is downloaded from the review page (not written to `output_dir`)
and fed back via `apply --decisions`.

## Status
Chunker, range expansion, reconcile (atomic master list), review page (status filters + editable rows
+ auto-save/import), apply, **ditamap-driven file list**, the **run version stamp** (source/pipeline/
chunked_at), and **`--auto-accept`** (judge / union) are built and tested end-to-end with `--mock-llm`.
The LLM audit and judge calls are wired for both backends but need credentials (USAi key, or Vertex ADC)
to run live.
