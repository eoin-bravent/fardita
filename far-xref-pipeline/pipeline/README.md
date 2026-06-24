# FAR ingestion pipeline (chunk → LLM audit → human review → verified)

One configurable pipeline that chunks a DITA regulation, audits its cross-references with
an LLM (blind), reconciles LLM vs. parser, lets a human resolve the differences in a
browser, and emits a provenance-tagged verified dataset. Stdlib-only (no SDKs; Python 3.14
safe). Runs on FAR today; `regulation` is configurable for DFARS/AFARS/etc.

## Stages
```
run:   resolve file set ─► chunk ─► manifest ─► blind LLM audit ─► reconcile ─► [optional LLM judge] ─► review.html
            (human opens review.html, accepts/overrides, exports decisions.json)
apply: merge approved ─► <REG>_verified.json   (every ref tagged with provenance)
```

## Commands
```
python pipeline.py run                              # full: chunk + Gemini audit + review page
python pipeline.py run --no-llm                     # PARSER ONLY: chunks + manifest, no audit/reconcile/review
python pipeline.py run --files 5.101 5.203 6.302-2  # run only these .dita files (names or paths)
python pipeline.py run --input-dir /path/to/dita    # run a different folder
python pipeline.py run --mock-llm refs.json         # drive reconcile/review from a canned LLM file
python pipeline.py run --limit 50                   # audit only the first 50 units (cheap smoke test)
python pipeline.py run --dump-payload 5.203         # print the exact prompt + raw .dita for one unit
python pipeline.py run --judge                      # also run the LLM judge to pre-fill review recs
python pipeline.py apply --decisions decisions.json
```
- **`--no-llm`** is the parser-only switch — it stops after `chunks` + `manifest` (no API key, no
  review page). **`--files`** picks specific files; otherwise the whole `input_dir` folder is scanned.
  **`--judge`/`--no-judge`** toggles the optional reconciliation pass.

## Row identity
Each row's `citation` is prefixed with the regulation — `FAR-5.101`, `FAR-6.302-2(a)` — and carries
a `regulation` field, so IDs stay unique across regulation sets (FAR vs DFARS vs AFARS). A
cross-reference `target` stays **bare** (`5.202(a)(2)`) since it's within the same regulation;
reconcile strips the prefix when matching.

## Cross-references
Each chunk's `cross_references` is a list **grouped by `target`** (one entry per distinct cited
citation); every textual occurrence is kept as a mention:
```json
{ "target": "5.207(c)", "confidence": "inferred",
  "mentions": [ {"kind": "inferred", "context": "…requirements of <xref href=\"5.207.dita#FAR_5_207\">5.207</xref>(c). The notice…"},
                {"kind": "inferred", "context": "…"} ] }
```
- `kind` (per mention) — `explicit` (a precise `<xref>` link) or `inferred` (resolved from a trailing
  qualifier, prose, or a range). `confidence` (per reference) = `explicit` if any mention is explicit,
  else `inferred`.
- `context` shows the source sentence with the raw `<xref>` markup inline, windowed past the reference.
- **Ranges are kept literal** as one reference — `target: "5.203(a)-(d)"`, `kind: inferred` — *not*
  expanded into members (expanding a span into individual edges is a later graph-stage decision).
- External U.S.C./URL references are excluded.

## Configuration — `pipeline.config.json`
| key | meaning | default |
|-----|---------|---------|
| `regulation` | label stamped on every row; names outputs | `FAR` |
| `input_dir` | folder of `.dita` files (relative paths resolve against this dir) | `../..` |
| `bottom_level` | deepest chunk level: `section`/`subsection` (unit only) · `paragraph` · `subparagraph` · `subunit-depth-1…4` | `paragraph` |
| `url_template` | source link, `{num}` filled with the citation | acquisition.gov/far/{num} |
| `output_dir` | where outputs land | `out` |
| `gemini.model` | model id (you set the highest available) | `gemini-2.5-pro` |
| `gemini.reasoning` | thinking on/off (on recommended for ambiguous refs) | `true` |
| `gemini.thinking_budget` | token budget (`-1` = dynamic) | `-1` |
| `gemini.judge` | optional 2nd LLM pass that pre-fills review recommendations | `false` |

Chunking goes from the file's own unit (section/subsection) **down to** `bottom_level`; parents
keep full text (overlap). Decomposition fields run `part … <bottom_level>`, bare, empty below
the chunk's level. Below `subparagraph` we use `subunit-depth-N` rather than inventing names.

## Configuration precedence
`CLI flags  >  .env / environment  >  pipeline.config.json  >  built-in defaults`

- **`.env`** (copy from `.env.example`, gitignored) holds the secret + common defaults:
  `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_REASONING`, `GEMINI_THINKING_BUDGET`, `GEMINI_JUDGE`, and
  optional `PIPELINE_REGULATION` / `PIPELINE_INPUT_DIR` / `PIPELINE_BOTTOM_LEVEL` / `PIPELINE_OUTPUT_DIR`.
- **CLI overrides** (on both `run` and `apply`): `--model --reasoning/--no-reasoning --judge/--no-judge
  --thinking-budget --regulation --input-dir --bottom-level --output-dir --config`.
- Real environment variables beat `.env`; `.env` beats the JSON; JSON beats defaults.

## LLM setup
- Put your **government key** in `.env` as `GEMINI_API_KEY=…` (or export it) — read from env only,
  never written to config/logs.
- **What each call sends:** the system prompt + the **entire raw `.dita` file** of that unit (not the
  flattened text), so the model sees the real `<xref href=…>` markup. Inspect the exact bytes with
  `python pipeline.py run --dump-payload <citation>`.
- **What the model returns:** one `target` per reference (ranges as one normalized span like
  `5.203(a)-(d)`, *not* expanded) with the quoted `evidence`. The prompt is attachment-aware — a
  parenthetical after a link *usually* narrows it but **not always** (e.g. `'the authority of 5.202
  and (a)(2) of this section'` → `5.202` **and** this section's `(a)(2)`). Its **highest-value job is
  prose references with no `<xref>` tag** (e.g. `'as required by 5.207'`) — the deterministic parser
  already catches every tagged link, so the prompt tells the model those untagged refs are exactly
  what it misses and to scan for them.
- **Optional LLM judge (`gemini.judge` / `--judge`):** a second pass, once per `.dita` file, that
  sees **only that file** — its raw source + that file's parser-vs-LLM discrepancies, *with both the
  parser's and the LLM's evidence* — and recommends a resolution + one-line rationale for each. It
  **pre-fills** the review page's Judge column (you bulk-accept or override) — it never finalizes, so
  the human gate stays. Off by default; moderate extra tokens.
- Calls Gemini's REST `generateContent` directly (urllib). Temperature 0, structured JSON
  output, `thinkingConfig` for reasoning. Responses are **cached** per unit by
  (model, prompt version, text hash) in `out/llm_cache/`, so re-runs are cheap and prior human
  decisions are never lost.
- **Rate limits matter**: ~2,964 units = one call each. On a low-RPM tier this is hours; on a
  paid/government tier, minutes. Backoff on 429/5xx is built in.

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

## Reconcile policy (locked)
- **corroborated** (LLM target == parser target) → auto-accept, not queued.
- **det-only** (parser found via `<xref>`, LLM didn't) → kept; markup is authoritative.
- **llm-new** (LLM found, parser missed) → **always** human review.
- **conflict** (same area, different resolved target — e.g. LLM `5.202` vs parser `5.202(a)(2)`)
  → human review.
The LLM never overrides; it only corroborates or raises a flag. Every LLM target is validated
against the FAR address map (grammar + existence); non-citations (U.S.C., URLs, hallucinations)
are dropped.

## Review page (`out/<REG>_review.html`)
Self-contained, no server. Per flagged item it shows three columns — **Parser** suggestion + evidence,
**LLM** suggestion + evidence, and (when `judge` is on) the **Judge** recommendation + rationale — a
link to the unit on **acquisition.gov** (new tab), and a choice: **Use parser / Use LLM / Manual**
(comma-separated citations) / **Reject**. If the judge ran, its recommendation is **pre-selected** (you
bulk-accept or change it). Click **Export decisions** to download `decisions.json`.

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
| `<REG>_chunks.json` | the chunks (pristine, parser-only) |
| `<REG>_manifest.json` | every file **seen**, **processed**, and **skipped** (with reasons) — the parser and LLM use this same set |
| `<REG>_queue.json` | flagged items for review (the `conflict` + `llm-new` buckets) |
| `<REG>_confirmed.json` | per-unit refs the LLM corroborated — consumed by `apply` for provenance |
| `<REG>_review.html` | the review page |
| `<REG>_verified.json` | after `apply`: chunks + human-approved refs, every ref tagged `provenance{producer, status}` (producer `parser`/`parser+gemini`/`gemini+human`/`human`; status `parser_only`/`corroborated`/`human_approved`) |
| `llm_cache/` | cached raw LLM audit + judge responses |

The reviewer's **`decisions.json`** is downloaded from the review page (not written to `output_dir`)
and fed back via `apply --decisions`.

## Status
Chunker, reconcile, review page (incl. the Judge column + auto-save/import), and apply are built and
tested end-to-end with `--mock-llm`. The Gemini audit and judge calls are wired but need
`GEMINI_API_KEY` to run live.
