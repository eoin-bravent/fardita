# acquisition.gov archive — parser eras (FAR + AFARS)

> **Multi-regulation:** the machinery is regulation-generic (`--regulation AFARS` on every
> command; profiles in `archive_adapter.REG_PROFILES` supply the URL template, ditamap name
> and dates file). The FAR eras are documented below; the **AFARS** archive
> (`archive_afars/AFARS`, 97 dated editions 2000→2025, dates in `afars_dates.json`) adds four:
>
> | era | folders | span | format | parser |
> |-----|--------:|------|--------|--------|
> | **dita** | 18 | 2022→2025 | ships actual DITA source zips (`AFARS.ditamap`) | `chunk_edition_dita` — runs the REAL `chunker.py`: canon, no adapter |
> | **ditaot** | 4 | 2022→2023 | DITA-OT XHTML render, `AFARS_PART_5101.html` | reuses the FAR ditaot parser (validated 474/500 byte-identical vs the DITA source) |
> | **agov** | 8 | 2021→2022 | Drupal/Word-export render (`Heading2/3/4` + plain `<p>`); 2 folders are 1–2-part partials | `chunk_edition_agov` |
> | **transit** | 67 | 1996→2019 | "HTML Transit" per-part files (`AFAR1.htm`/`5101.htm`), legacy-style bold headings; the 2018–19 folders are 1–3-part partials | `chunk_edition_transit` (reuses the legacy machinery) |
>
> AFARS specifics: its DITA writes paragraph labels as literal text in top-level `<p>`s (no
> `<ol>/<li>`), so the canon is **unit rows only** (`bottom_depth: 0` in the profile — every
> era matches, and paragraphs live as lines in the unit text). **Partial editions** merge with
> `complete=False` (the driver auto-detects via `--partial-threshold`, default 0.5: add/update
> only, nothing closed). **Bootstrap order matters**: run `--eras dita` first (canon + breadcrumb
> titles), then `--eras ditaot,agov,transit` backfills against it (hints supply titles;
> collapse-cosmetic handles the cross-generator seams). The earliest editions (96-x) use
> pre-renumbering part numbers (1.xxx) — their sections close at the 5101-renumbering,
> which is historically accurate.

## FAR eras

The `archive_far/` download is **290 edition folders** spanning FAC 1990-34 (≈1995) through
FAC 2025-06. They are not one format: GSA re-tooled its HTML publishing pipeline several
times, so the markup falls into a handful of distinct **eras**, each of which needs its own
parser. This is the map for building those parsers.

An *era* = one HTML-generator lineage = one parser. What separates them is not cosmetic: the
**file granularity** (one HTML file per *part* vs per *subpart*), how a **section boundary**
is marked (a class, an autonumber span, or just bold text), and whether **cross-references**
are real `<a href>` links or plain prose. A parser written for one era's conventions produces
garbage on another.

Regenerate this classification any time with:

```bash
python archive_adapter.py survey ../archive_far      # writes archive_eras.json
```

`archive_eras.json` has one record per folder: `era`, `variant`, `html_root` (the subdir
that actually holds the HTML — some downloads nest it), `html_files`, and the `fac` +
`effective_date`.

**Effective dates — `archive_dates.json`.** Every edition's authoritative published effective
date (scraped from `acquisition.gov/archives?type=FAR`, all 34 pages), keyed by folder name —
332 entries covering all 290 folders. `parse_meta(folder)` reads it first (falling back to the
ditaot `LSATable.html` parse), so `ingest --edition-dir <folder>` fills `--date`/`--source-version`/
`--commit` automatically — no manual date entry, for any era. Cross-checks the store exactly on the
overlap (2023-02 → 2023-03-16). Two quirks it handles: the `2005-0` folder is an epoch placeholder
(`1969-12-31`, skipped), and a couple of folder-name collisions are resolved by matching the FAC
number to the folder. Date spans by era: legacy 1995–2002, webworks-2001 2001–2005, webworks-2005
2005–2018, ditaot 2021–2025.

## The five groups

| era | folders | FAC span | granularity | section heading | refs | parser |
|-----|--------:|----------|-------------|-----------------|------|--------|
| **ditaot** | 27 | 2021-07 → 2025-06 | one file per **part** (`FAR_Part_5.html`), sections as nested `<article>` | `<span class="ph autonumber">5.101</span>` | `<a href>` links | **built** (`chunk_edition_ditaot`) |
| **webworks-2005** | 168 | 2005-00 → 2005-99 | one file per **subpart** (`Subpart 5_1.html`) | `<h3 class="pSection">` / class `pSection` | `<a href>` links | **built** (`chunk_edition_webworks2005`) |
| **webworks-2001** | 42 | 1997-27 → 2001-27 | one file per **subpart** (`Subpart_5_1.html`) | `<h2 class="Heading1">` | **plain text** | **built** (`chunk_edition_webworks2001`) |
| **legacy** | 52 | 1990-34 → 1997-27 | one file per **part** (`html/05.html`, `05PART.HTM`) | `<H3><B>5.000 …</B>` or bare `<B>` text | **plain text** ("see 5.201") | **built** (`chunk_edition_legacy`) |
| **fm-source** | 1 | 2005-79 | — FrameMaker `.fm` source only, **no HTML** | — | — | not parseable as-is |

The counts sum to 290. Editions that already backfilled into the store (nine ditaot editions,
FAC 2022-01 → 2023-01) all come from the first row.

## Per-era detail (what a parser must handle)

### ditaot (2021-07 → 2025-06) — done
DITA-OT XHTML, the same DITA lineage as the current GitHub repo. One `FAR_Part_N.html` per
part; each section/subsection is a nested `<article>` with a `<div class="body">`. Paragraph
labels are `<span class="ph autonumber">(a)</span>`; the flat `ListL1/2/3` classes encode
*visual* depth, which disagrees with structural depth — resolved by the store-derived hints
(see README "acquisition.gov archive adapters"). Cross-refs are `<a href="#FAR_5_207">` /
`<a href="FAR_Part_47.html#FAR_47_507">`. Alternates live in a trailing
`<section class="section Alternate">`; the clause terminator is `class="…Endofclause">`.
**Reference implementation** in `archive_adapter.py`.

### webworks-2005 (the big one — 168 folders, all "FAC 2005-xx") — **built**
WebWorks/Halvik XHTML 1.0. **One file per subpart** — `Subpart 5_1.html` holds every section
of subpart 5.1; Part 52 clauses are grouped (`52_000_107.html` = clauses 52.000–52.107). ~460
HTML files per edition. Flat sibling structure under `<body>`: `<h3 class="pSection">5.000  Scope
of part.</h3>` opens a section, `<p class="pBody">` is a top-level paragraph, `<p class="pIndentedN">`
is depth N (**structural**, unlike ditaot's visual `ListLn`), `<p class="pBodyCtr*">` are centered
clause lines (dated SmCaps title, `(End of clause)`), `<h2 class="pSubpart">` a subpart head.
Paragraph labels `(a)(1)(i)` are **literal text**; `<a name="wp…">` are position anchors (drop);
cross-refs are `<a href="Subpart 5_2.html#wp…">5.201</a>` with the citation in the **anchor text**
(the href is a useless wp-anchor). Alternates follow the End marker as `Alternate I …` paragraphs
(no wrapper section). Nav tables (`id="SummaryNotReq…"`, navprev/navnext gifs) are stripped.

Implementation notes (all in `archive_adapter.py`, `chunk_edition_webworks2005` + `_ww_*`):
- Section identity: parse the leading number out of the `pSection` heading text (never the
  filename — the file is a whole subpart).
- **Different source lineage** than the DITA store, so exact text legitimately differs at the
  2005↔2021 seam (dash glyphs `—`/`–` vs `-`, `18 U.S.C. 4124` vs the DITA's `U.S.C.4124`,
  `(d) (1)` vs `(d)(1)`, quotes around defined terms). The parser matches the chunker's
  **conventions** (whitespace, table→HTML, `[IMAGE:]`, title stripping, alternates, clause meta),
  not another generator's glyphs — see the seam caveat below.
- Hints (store row-labels, optional via `--hints-store-dir`) reject a handful of nested list
  items the FrameMaker source mis-renders at `pBody` depth (e.g. a few numbered sub-items in the
  2.101 definitions), which would otherwise fabricate duplicate/spurious top-level rows.
- Watch the folder-name noise: `FAC 2005-94,2005-95`, `FAC FAR 2005-83`, `FAC FAC2005-86`,
  `…-2-08032015`. Names are unreliable; get the FAC/date from a cover or `FARCorrection.html`.

**Validation (against store `as_of(2021-12-06)`, a 13-year cross-source gap):** FAC 2005-99 →
11,254 rows, 0 duplicate identities, 0 malformed citations, all parts. **4,743 sections byte-identical**
to the DITA store (proving the flattening conventions match); a further ~4,000 differ only by the
cross-source rendering artifacts above; ~2,000 are real 2005→2021 edits; 102 sections are 2005-only.
Generalizes across the era (FAC 2005-30 ≈2009: 10,356 rows, 0 dups). The field-level diffs
(`subsection_title` 936 = em-dash rendering; `date`/`instrument`/`end_marker`/`prescribed_by` =
real 13-year clause revisions, marker corrections, renumberings) are source differences, not bugs.

**The cross-source seam, and `--collapse-cosmetic`.** Because 2005 is FrameMaker and 2021+ is
DITA, the same legal text renders with different typography (dash glyphs `—`/`–` vs `-`,
`U.S.C. 637` vs DITA `U.S.C.637`, `(d) (1)` vs `(d)(1)`, quotes around defined terms). A naive
ingest would therefore create one large batch of "changed" rows at the single 2005↔2021 boundary
that reflect rendering, not amendments. (*Within* the 2005 era — one generator — consecutive
editions are self-consistent, so backfilling 2005-00 … 2005-99 among themselves is already clean.)

`ingest --collapse-cosmetic` handles the boundary. It uses a canonicalizer as a **classifier, not
a rewriter**: a snapshot chunk that is cosmetic-equal to the identity's earliest existing store row
(every structural/metadata hash field exactly equal, and text+titles equal after canonicalizing
dashes/whitespace) is snapped to that row's content so the merge **extends it backward** (no new
version); a chunk that differs meaningfully is ingested **verbatim** as a real historical row. Every
snap is written to an audit log (`<rows>_collapsed.json`). Conservative by default: any difference
in `date` / `instrument` / `prescribed_by` / `end_marker` / `reserved` / `images` counts as
meaningful (a clause revision changes its date, so it stays a real version). `--collapse-drop-quotes`
additionally treats quote-around-term differences as cosmetic (off by default — small effect,
~1% of rows, and arguably a real FAR restyling).

**Validated** (FAC 2005-99 into a store copy, cosmetic default): of 11,152 shared identities,
4,743 already identical + **3,529 collapsed** cosmetic → extend backward with no new row; **2,880**
real differences ingested verbatim as `[2005, 2021)` historical versions; 2005-only sections added
as new. On a Parts 5/22/52 slice the collapse cut typography-only version rows from 2,487 to 1,061
(−57%), current LLM-verified rows were untouched (3,861→3,861, 0 content changes), and invariants
stayed clean. Audit-log spot check: every collapsed section differed only by em-dash/spacing, no
legal change.

### webworks-2001 (42 folders, "FAC 1997-27n" → "FAC 2001-xx") — **built**
Earlier Quadralay WebWorks (HTML 4.0 Transitional). **One file per subpart** —
`Subpart_5_1.html` (underscore, no space — distinct from the 2005 spacing). Content sits inside
a `<blockquote>`; a section opens with `<h2 class="Heading1">` (subpart heads are
`<h3 class="Heading2">`); unlabelled section text is `<p class="Body">`; paragraph lists are
`<dt class="IndentedN">` items in a `<dl>` — with N **offset by one** from 2005 (`Indented1` =
the top-level `(a)`, `Indented2` = `(1)`). A clause's dated title and its `(End of clause/provision)`
marker are **center-styled `<div>`s** (inline style, no class). There are **no internal cross-ref
links** — references are plain prose (so `cross_references` are range/prose only; refs aren't hashed
anyway).

Implementation (`chunk_edition_webworks2001` + `_w1_*`, reusing the webworks-2005 flatten/table/
label/ref helpers):
- `<dt>`/`<dd>` omit their close tags, so lxml **nests** them; `_w1_sections` recovers the true
  order via `dl.iter()`, and the shared flatten/scope helpers stop at nested dt/dd (fixed for both
  eras). Depth comes from the `IndentedN` class, never the (bogus) tree nesting.
- Some editions ship **both** a space- and underscore-named copy of a subpart (`Subpart 5_6.html`
  AND `Subpart_5_6.html`); `_ww_files` now dedupes them (else every section parses twice).
- Different **source generator** than webworks-2005, so the new cross-generator artifact is
  **straight vs curly quotes** (2001 `"…"` vs 2005 `"…"`) on top of the usual dash/spacing — all
  handled by `collapse_cosmetic`'s canonicalizer.

**Validation** (cross-adapter: webworks-2001's newest edition FAC 2001-27 (Jan 2005) vs
webworks-2005's oldest FAC 2005-01 (Apr 2005), 3 months apart): 10,400 rows, 0 duplicate
identities, 0 malformed, 49 parts; near-identical identity sets to the 2005 adapter (only 10 / 2
unique). **4,707 sections byte-identical** across the two generators, a further 5,069 collapse as
cosmetic → **94% same legal content**; the remaining ~600 are real 3-month amendments and
generator table-rendering differences (e.g. 52.212-3's Jan→Mar 2005 revision). Clause metadata
(dated titles, end markers, prescribed-by, 185 alternates) all parse correctly.

### legacy (52 folders, "FAC 1990-34" → "FAC 1997-27" + "1997 Reissue") — **built**
Hand-authored / early-tool plain HTML, **no CSS classes at all**. **One file per part** —
`html/05.html` is the entire Part 5; older editions use `html/5.html` or `05PART.HTM`; later
ones add `52_NNN.html` clause groups. Section boundaries are bold/heading runs
(`<H4><B>5.000  Scope of part.</B></H4>`, bare `<P><B>…</B>`, or `<DIV CLASS=9SecHdg><FONT><B>…`
— all three occur, sometimes within one edition). Paragraphs are flat `<P>`s with literal labels
and **no depth markers whatsoever**; refs are plain prose (never `explicit`). Each file opens
with a TOC (dropped — no section is open yet).

Implementation (`chunk_edition_legacy` + `_lg_*`, sharing the webworks flatten/table/ref helpers):
- **The critical parse fix**: the era never closes its `<A NAME>` anchors, so lxml nests each
  inside the previous until **libxml2's 255-depth cap silently drops the rest of the file**
  (part 32 of FAC 1997-27 lost everything past 32.900). `_lg_parse` self-closes the anchors
  (regex tolerant of `NAME ="…"` spacing variants) before parsing.
- Heading rule: an element whose *visible* flatten equals its first bold run's text (tolerates
  `<FONT>` wrappers, trailing `.` outside the `</B>`, anchor-swallowed tails; titles up to 250
  chars — FAR clause titles run long).
- **Row (depth-1) detection with no depth markers**: hints decide for store-known units; for dead
  units the FAR's own label ladder — depth 1 is always lowercase letters; single-char
  roman-lookalikes ((i),(v),(x)) are rows only when they continue the letter sequence.
- Clause meta: dated titles are centered `<P>`s; End markers text; alternates via the usual opener.
- Source defects surface as duplicate identities (e.g. FAC 1990-34's `17.html` contains subpart
  17.5 **twice**, once even in two different versions — a 1995 publication error). The merge
  dedupes first-wins and reports `duplicates_in_snapshot`.

**Validation — the gold pair**: FAC 1997-27 exists in BOTH formats (this era's `FAC 1997-27` and
webworks-2001's `FAC 1997-27n`, same effective date 2001-06-25). Cross-parse: 10,253 vs 10,232
rows, **identity sets align to 10,222 shared (only 31/10 unique)**, 0 duplicates, 0 malformed;
**5,536 byte-identical + 3,381 cosmetic = 87% same content**; the remaining ~1,300 are *genuine
editorial differences between the two publications* (the "n" re-issue re-edited wording:
"subparagraph (b)(6)" → "paragraph (b)(6)", "6.502 below" → "6.502" — verified by sampling), plus
soft-hyphen artifacts now handled by the canon. The oldest B-variant edition (FAC 1990-34, 1995)
parses cleanly too: 9,850 rows, 49 parts, clauses/alternates detected.

### fm-source (1 folder: FAC 2005-79)
Contains only FrameMaker `.fm` binaries under `FrameMaker/FAR BOOK/`, no rendered HTML.
Not parseable by an HTML adapter. Options: skip it (2005-79 is one edition among 168 in its
era and the surrounding editions cover the same text), or convert the `.fm` files to HTML
first. Recommend skip.

## Suggested build order

1. ~~**webworks-2005**~~ — **done.** 168 editions, the largest block; unlocks 2005–2018.
2. ~~**webworks-2001**~~ — **done.** 42 editions; unlocks 2001–2005.
3. ~~**legacy**~~ — **done.** 52 editions; unlocks 1995–2002. **All four HTML eras now have
   parsers** — every parseable archive edition (289 of 290 folders; the one exception below)
   can be chunked, seam-checked and backfilled with the same commands.
4. **fm-source** — skip unless FAC 2005-79 specifically is needed (FrameMaker source only).

**Validating a cross-source era (no store overlap before 2021).** The seam test still uses
`store.as_of(2021-12-06)`, but you don't expect ≈0 `changed` like the same-source ditaot backfill.
Instead the signal is: (a) a large **byte-identical** count (proves your flattening conventions
match the chunker), (b) mismatches **concentrated** in real edits + systematic rendering artifacts
(dashes, spacing) rather than spread uniformly (which would mean a normalization bug), and (c) 0
duplicate identities / 0 malformed citations. The `seam` report's per-field breakdown and samples
are built for exactly this triage.

## Backfilling a whole era — `backfill_archives.py`

The driver loops an era's editions **oldest-first** (chronological, by authoritative date) and,
keeping one Store in memory (saved after each edition, so it's crash-safe and resumable), runs
`chunk → collapse-cosmetic → merge_snapshot → verify` per edition. It writes a combined collapse
audit (`backfill_collapse_audit.json`, every snapped identity per edition) and a per-edition report
(`backfill_report.json`, merge stats + verify status + timing). Editions already in the store's
edition registry are skipped, so a re-run resumes. Structure hints are derived once. Defaults to
webworks-2005 (168 editions).

```bash
python backfill_archives.py --store-dir store_copy --plan        # print the plan, ingest nothing
python backfill_archives.py --store-dir store_copy               # backfill webworks-2005, oldest-first
python backfill_archives.py --store-dir store_copy --limit 5     # first 5 (oldest) only — smoke test
python backfill_archives.py --store-dir store_copy --no-collapse # verbatim, no cosmetic collapse
```

Always run it into a **store copy** first and check `backfill_report.json` (all `verify_ok`, sane
stats) before pointing it at the live `store/`. The collapse baseline is order-independent — it
compares each chunk against the row the merge would abut (the in-force row or the next-newer one),
so a section that stabilizes mid-era folds into the future DITA rendering rather than opening a
spurious 2005↔2021 boundary.

## Verifying any corpus — `corpus_audit.py` (text conservation)

For scaling to new agency supplements, the completeness proof is **conservation of text**:
every substantive segment of the source HTML must appear in some chunk row or be explicitly
classified as skipped-by-design (nav, TOCs, headings-captured-as-titles, TOC entries for the
parent regulation's sections, page furniture). Whatever survives classification is
**UNCLASSIFIED residue** — the alarm metric — reported with per-file counts and samples.

```bash
python archive_adapter.py chunk <edition_dir> -o rows.json --regulation X …
python corpus_audit.py <edition_dir> --rows rows.json [--report audit.json]  # PASS/FAIL
```

Era- and regulation-agnostic (canonical token shingles forgive cross-generator dash/quote/
space artifacts; a title pool covers 'NUM Title.' heading lines). Current results: FAR
webworks-2005 FAC 2005-99 → **0.006%** unclassified over 6.0M source chars; AFARS transit
2013-26 → 0.209%. On its FIRST run it caught a real silent-loss bug the store-seam couldn't
see (webworks styles the `* * * * *` elision separator inside clause Alternates as an
`h2.pSubpart`, which the parser treated as a subpart boundary — dropping Alternates III–V of
the advance-payment clauses; they didn't exist in the 2021 store, so no diff ever flagged
them), plus AFARS' three-level citations (`5101.602-2-91`) the citation grammar rejected.

The full verification stack for a new corpus, strongest first: (1) this conservation audit
per edition; (2) publisher-manifest cross-check (ditamap / TOC pages vs parsed sections);
(3) cross-rendering differential when an edition ships two formats (AFARS ditaot-HTML vs
DITA source: 474/500 byte-identical proves both parsers at once); (4) cross-edition
statistics (uniform diffs = parser bug, concentrated diffs = real amendments; per-part
char-count cliffs = dropped text); (5) structural + store invariants (citation grammar,
duplicates, `verify_store`). An LLM is only needed to triage the exception queue these
checks emit — not to read the corpus.

## The parser contract (same for every era)

Each era's parser is a `chunk_edition_<era>(edition_dir, cfg, hints=None)` that returns the
standard chunk rows — identical field shape to `chunker.py` (`demo_fields.py` /
`VERIFIED_FORMAT.md`), fields the old format can't supply left as `''`/`[]`. Then the
**shared** commands do the rest, unchanged per era:

```bash
python archive_adapter.py seam   rows.json --store-dir <COPY> --date <D>   # HASH_FIELDS parity
python archive_adapter.py ingest rows.json --store-dir <COPY> --date <D> \
       --source-version "FAC …" --commit <folder>
```

The non-negotiable is **text-normalization parity over `store.HASH_FIELDS`**: flatten the
same legal text the same way `chunker.py` does (whitespace, tables→HTML, `[IMAGE: id]`,
title period-stripping, label spacing) or a backfill fabricates thousands of spurious
"changed" rows at the seam. `seam` is the tool that measures it; iterate the parser until it
reports ≈all `unchanged`/`extended_backward`, near-zero `changed`, before ingesting for real.
