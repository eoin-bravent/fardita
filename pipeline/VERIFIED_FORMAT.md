# `FAR_verified.json` structure

`FAR_verified.json` is a JSON **array of chunks**. Each chunk is one piece of the FAR — produced by
splitting the DITA source from the unit down to the configured bottom level. Every chunk carries its
identity, its text, and the references found in it.

A chunk's identity is the pair **`(citation, alternate)`**. Three independent axes describe it:
- `type` — **structural** level (FAR 1.105-2): `section` / `subsection` / `paragraph` / `subparagraph` / …
- `kind` — **functional** instrument: `clause` / `provision` / `""` (an ordinary regulatory section).
  Propagated to every chunk of an instrument (a clause's paragraphs are `kind:"clause"` too).
- `alternate` — **variant**: `""` for the base text, or an arabic id `"1"`/`"2"`/… for a clause Alternate.

**Each chunk has:**
- `citation` — its identifier, prefixed with the regulation (`"FAR-5.101"`, `"FAR-6.302-2(a)"`). A clause
  and its Alternates **share one citation**, distinguished by `alternate` (an Alternate is its own flat
  chunk, e.g. `citation:"FAR-52.247-64", alternate:"1"`, with `type` inherited from the base).
- `regulation` / `type` / `kind` / `alternate` — `"FAR"` plus the three identity axes above.
- `source_version` — the FAR edition the chunk came from, verbatim from the DITA map's `rev`
  (e.g. `"FAC 2026-01 March 13, 2026"` — Federal Acquisition Circular number + effective date).
- `pipeline_version` — the git short SHA of the code that produced the chunk (e.g. `"e3e4eee"`).
  Together these two are the chunk's provenance; the run timestamp lives in `<REG>_manifest.json` (`chunked_at`).
- `changes` — **change track**: a list of the `rev`-marked spans (this FAC's edits) that fall inside this
  chunk; `[]` when nothing changed. Each item: `text` (the exact changed words — find it in `text` to
  redline), `fac` (the FAC, from the `rev` attribute), `case_number` and `why` (from the inline
  `[CaseNumber]`/`[Why]` markers next to the span; `why` may be empty for spans the source doesn't
  describe individually, e.g. table-cell edits). A change appears on **every chunk that contains it**
  (the section chunk and the specific paragraph). The complete section-level description lives in
  `<REG>_changelog.json`; `changes` carries the span-level detail.
- decomposed address — `part`, `subpart`, `section`, `subsection`, `paragraph`, … (the citation broken into levels).
- `url` — the source page on acquisition.gov.
- `text` — the chunk's text.
- `cross_references` — references to **other parts of the FAR** (see below).
- `external_references` — references to **other government documents** (see below).
- Tables aren't a separate field — the **table content is inlined into `text` as clean HTML**
  (`<table><caption>…</caption><thead>…<tbody>…`, with `colspan` for merged header cells), so an LLM
  reading the chunk sees the full table.
- `images` — a deduped list of **image ids** present in the chunk, e.g. `["piid.png"]`. In `text` each
  appears as an inline placeholder `[IMAGE: piid.png]` marking its position. The image's **binary and
  plain-language `description` live in a separate downstream store keyed by that id** (not duplicated on
  every chunk that uses the image); the id is the image's filename.
- (A paragraph chunk inherits any table/image inside it, just as it inherits that text.)
- `end_marker` — on the **base** clause/provision chunk: the terminator, canonicalized to `"(End of clause)"`
  or `"(End of provision)"` (from the source `@outputclass`, whose raw text varies in casing/parens). `""` on
  ordinary regulatory sections, paragraphs, and Alternate chunks. The marker is **stripped from `text`** (no
  retrieval signal, uniformly present); re-insert it as a delimiter at prompt-assembly time if wanted.
- `date` — effective date of this clause version, as written and space-normalized (`"Nov 2021"`). Populated on
  clause/provision base and Alternate chunks (from the dated title / the Alternate's opener); `""` elsewhere.
- `prescribed_by` — the FAR section prescribing this clause version, parsed from "As prescribed in …"
  (`"47.507(a)"` for a base clause, `"47.507(a)(2)"` for its Alternate 1). `""` on ordinary sections,
  paragraphs, and the older Alternates that use conditional phrasing instead ("If a cost contract…").
- `reserved` — `true` when the chunk is just a placeholder (e.g. `Alternate I [Reserved]`).

**Alternates are flat chunks, not nested.** Each Alternate of a clause is its own row, sharing the base's
`citation` and distinguished by `alternate` (`"1"`, `"2"`, …), with `type`/`kind` inherited from the base. Its
`text` is the alternate's **verbatim** content (the literal "Alternate I (date). …" plus any substitute/added
paragraphs) — **stored as-is, not reconstructed** (the delete/substitute/add instructions are not applied to
the base). Its `cross_references` / `external_references` / `images` / `changes` are scoped to that alternate
and are **parser-derived** (`parser_only` status — the file-level LLM audit runs on the base unit only). The
base clause's `text`/refs/images/changes **exclude** the alternates. Resolve "clause X Alternate N" by looking
up the chunk with `citation == X` and `alternate == N`.

**`cross_references`** (internal, FAR → FAR) — each entry:
- `target` — the cited FAR citation, bare (`"5.202(a)(2)"`, `"subpart 9.1"`). Ranges are pre-expanded
  into individual members, so each target is atomic.
- `confidence` — `explicit` (came from a tagged `<xref>` link) or `inferred` (resolved from prose / a range).
- `alternate` — the clause **Alternate** this reference points to: `""` for the base clause, or an arabic id
  (`"1"` for *"52.204-30 Alternate I"*). `target` stays the base clause; `(target, alternate)` is the edge, so
  a reference to a clause and to its Alternate are **distinct**. Resolve it to the matching
  `(citation, alternate)` chunk.
- `mentions` — each occurrence, with its `kind` and `evidence` (the surrounding source sentence).
- `status` — how it was verified: `corroborated` (parser and LLM agreed), `parser_only`, `human_approved`,
  or `auto_accepted` (accepted by an `--auto-accept` run — judge verdict or parser∪LLM union — without a human pass; still auditable).

**`external_references`** (other government documents) — each entry:
- `ref_type` — `usc` / `cfr` / `eo` / `public_law` / `omb` (statutory citations), `form` (Standard /
  Optional / DD forms), or `url` (any other tagged external link).
- `target` — a canonical id, so repeated references to one document share one node: `usc:41/1303`,
  `cfr:13/128.300`, `eo:11246`, `publ:118-31`, `omb:A-76`, `form:SF-33`; for a `url` it's the URL itself.
- `locator` — the specific subsection cited (e.g. `"(a)(4)"`), kept separate from the document id.
- `node_label` / `citation` — human-readable form (`"41 U.S.C. 1303"`, `"Standard Form 33"`) and the
  verbatim text as written.
- `href` — a resolvable link when the source had one (gov form pages, `uscode.house.gov`, etc.); `""` if none.
- `division_levels` — the citation parsed into ordered parts (e.g. `["41","1303","a","4"]`, `["SF","33"]`).
- `mentions` / `status` — same as internal.

Statutory citations are found by regex in the prose; **forms and URLs come from tagged `<xref>` links**
in the source (so they're high-precision), and a tagged link's `href` is also attached to the statutory
reference it points to.
