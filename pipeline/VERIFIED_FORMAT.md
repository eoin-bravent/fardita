# `FAR_verified.json` structure

`FAR_verified.json` is a JSON **array of chunks**. Each chunk is one piece of the FAR — a `section`,
`subsection`, or `paragraph` — produced by splitting the DITA source from the unit down to the
configured bottom level. Every chunk carries its identity, its text, and the references found in it.

**Each chunk has:**
- `citation` — its identifier, prefixed with the regulation (`"FAR-5.101"`, `"FAR-6.302-2(a)"`).
- `regulation` / `type` — `"FAR"` and `section` / `subsection` / `paragraph`.
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

**`cross_references`** (internal, FAR → FAR) — each entry:
- `target` — the cited FAR citation, bare (`"5.202(a)(2)"`, `"subpart 9.1"`). Ranges are pre-expanded
  into individual members, so each target is atomic.
- `confidence` — `explicit` (came from a tagged `<xref>` link) or `inferred` (resolved from prose / a range).
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
