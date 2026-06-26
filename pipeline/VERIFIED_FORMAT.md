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
- `tables` / `images` — any tables/figures in the chunk. These are **omitted from `text`** (where they
  appear as `[TABLE OMITTED …]` / `[FIGURE OMITTED …]` placeholders) and recorded here instead:
  tables as `{caption, url}`, images as `{href, alt, url}`. The `url` points at the source page on
  acquisition.gov so the real table/figure can be retrieved. (A paragraph chunk inherits the media of
  any table/figure inside it, just as it inherits that text.)

**`cross_references`** (internal, FAR → FAR) — each entry:
- `target` — the cited FAR citation, bare (`"5.202(a)(2)"`, `"subpart 9.1"`). Ranges are pre-expanded
  into individual members, so each target is atomic.
- `confidence` — `explicit` (came from a tagged `<xref>` link) or `inferred` (resolved from prose / a range).
- `mentions` — each occurrence, with its `kind` and `evidence` (the surrounding source sentence).
- `status` — how it was verified: `corroborated` (parser and LLM agreed), `parser_only`, or `human_approved`.

**`external_references`** (other gov docs: U.S.C., CFR, E.O., Public Law, OMB Circular) — each entry:
- `target` — a canonical id for the document/section (`usc:41/1303`, `cfr:13/128.300`, `eo:11246`,
  `publ:118-31`, `omb:A-76`), so repeated citations to one document share one id.
- `ref_type` — `usc` / `cfr` / `eo` / `public_law` / `omb`.
- `locator` — the specific subsection cited (e.g. `"(a)(4)"`), kept separate from the document id.
- `node_label` / `citation` — human-readable form (`"41 U.S.C. 1303"`) and the verbatim text as written.
- `division_levels` — the citation parsed into ordered parts (e.g. `["41","1303","a","4"]`).
- `mentions` / `status` — same as internal.
