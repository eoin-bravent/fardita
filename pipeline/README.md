# FAR ingestion pipeline (chunk → LLM audit → human review → verified)

One configurable pipeline that chunks a DITA regulation, audits its cross-references with
an LLM (blind), reconciles LLM vs. parser, lets a human resolve the differences in a
browser, and emits a provenance-tagged verified dataset. Stdlib-only (no SDKs; Python 3.14
safe). Runs on FAR today; `regulation` is configurable for DFARS/AFARS/etc.

## Workflow
1. **`run`** — chunk the regulation, find its references two ways (the deterministic **parser** and a
   blind **LLM audit**), reconcile them, and write the review page. Add `--judge` to have the LLM
   pre-fill a recommendation for each disagreement.
2. **`review`** — serve the page in a browser; accept / reject / fix the flagged references, then click
   **Save & Apply** — that writes your decisions and builds the verified dataset in one click.
3. **result** — `out/<REG>_verified.json`: every reference tagged with where it came from and a status.

```
run:   chunk ─► parser refs + LLM audit ─► reconcile ─► [optional judge] ─► review page
review: you accept/reject/fix in the browser ─► Save & Apply ─► <REG>_verified.json
```
(`apply --decisions <file>` is the manual alternative to Save & Apply — feed it an exported decisions.json.)

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
python pipeline.py run --concurrency 16              # parallel LLM calls (default 8; 1 = sequential)
python pipeline.py review                            # serve the review page; "Save & Apply" writes verified.json in one click
python pipeline.py apply --decisions decisions.json  # (manual path: feed an exported decisions.json)
```
- **`--no-llm`** is the parser-only switch — it stops after `chunks` + `manifest` (no API key, no
  review page). **`--files`** picks specific files; otherwise the whole `input_dir` folder is scanned.
  **`--judge`/`--no-judge`** toggles the optional reconciliation pass.
- **`--concurrency N`** runs N audit/judge calls in parallel (the audit is the slow stage — a full
  corpus is many hours sequential, ~N× faster threaded). Tune down if you hit provider rate limits
  (429s back off automatically). Subset (`--files`) runs build a **whole-corpus address map** (cached
  to `out/<REG>_addrmap.json`) so cross-file targets still validate.

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
  "mentions": [ {"kind": "inferred", "evidence": "…requirements of <xref href=\"5.207.dita#FAR_5_207\">5.207</xref>(c). The notice…"},
                {"kind": "inferred", "evidence": "…"} ] }
```
- `kind` (per mention) — `explicit` (a precise `<xref>` link) or `inferred` (resolved from a trailing
  qualifier, prose, or a range). `confidence` (per reference) = `explicit` if any mention is explicit,
  else `inferred`.
- `evidence` (per mention) shows the source sentence with the raw `<xref>` markup inline, windowed past
  the reference. (Same field name as the LLM/ledger evidence, for alignment.)
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
  `publ:<cong>-<num>`, `omb:<series>-<num>`. The node is the doc/**section**; the precise sub-part rides
  on the edge as **`locator`**, so many citations to different subsections of one statute collapse to one node.
- **`division_levels`** is the full parse (title, section, subsections…), mirroring the DITA decomposition.
- **`ref_type`** ∈ `usc | cfr | eo | public_law | omb` — only these **five strict formats** are kept.
  Named statutes ("the Small Business Act"), program/agency names, treaties, standards, etc. are
  **deliberately excluded**: regex-detecting named acts produced far too many false hits (line-wrapped
  names fragmenting into many nodes, capitalized phrases over-matching). Recoverable later via a curated
  act whitelist if needed.
- **Parser** handles the five formats (high-precision regex → `confidence: explicit`, auto-kept). **LLM**
  is restricted to the same five and told not to report anything else; any LLM external that doesn't
  parse to one of the five is dropped at reconcile. LLM-only externals get human review; the
  internal-framed LLM judge does **not** run on externals.
- **Editing**: on the review page, an external row's **Manual** option gives two fields — **Document**
  (the node, e.g. `Small Business Act` or `41 U.S.C. 1303`) and **Section** (the locator, e.g. `8(a)`) —
  so you correct the document and the section independently; `apply` rebuilds the canonical edge.

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
- **CLI overrides** (on both `run` and `apply`): `--model --reasoning/--no-reasoning --judge/--no-judge
  --thinking-budget --regulation --input-dir --bottom-level --output-dir --config`.
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
(corroborated/explicit are shown read-by-default but stay editable). A former "conflict" (LLM `5.202`
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
| `<REG>_chunks.json` | the chunks (pristine, parser-only) — each row has `cross_references` (internal) + `external_references` |
| `<REG>_manifest.json` | every file **seen**, **processed**, and **skipped** (with reasons) — the parser and LLM use this same set |
| `<REG>_ledger.json` | the per-unit master list: every atomic target tagged `status` (corroborated / parser_explicit / parser_inferred / llm_only), with parser/llm/judge evidence — drives the review page and `apply` |
| `<REG>_token_usage.json` | per-run token usage (prompt/thinking/output/total by stage, per-unit), timing, status counts, cache hits |
| `<REG>_addrmap.json` | cached whole-corpus address map (so `--files` subset runs validate cross-file targets) |
| `<REG>_review.html` | the review page |
| `<REG>_verified.json` | after `apply`: chunks + human-approved refs (both `cross_references` and `external_references`), every ref tagged with a flat `status` (`parser_only` / `corroborated` / `human_approved`) |
| `llm_cache/` | cached raw LLM audit + judge responses |

The reviewer's **`decisions.json`** is downloaded from the review page (not written to `output_dir`)
and fed back via `apply --decisions`.

## Status
Chunker, range expansion, reconcile (atomic master list), review page (status filters + editable rows
+ auto-save/import), and apply are built and tested end-to-end with `--mock-llm`. The LLM audit and
judge calls are wired for both backends but need credentials (USAi key, or Vertex ADC) to run live.
