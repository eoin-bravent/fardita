# FAR cross-reference pipeline

Chunks the **Federal Acquisition Regulation (FAR)** DITA source, audits its cross-references with
an LLM (via [USAi.gov](https://www.usai.gov/), the GSA government AI platform), reconciles the LLM
against a deterministic parser, lets a human resolve the differences in a browser, and emits a
provenance-tagged verified dataset. Stdlib-only Python (no SDKs).

## Layout
```
dita/         FAR DITA source — the government download (3,902 .dita files + DITA toolchain artifacts)
pipeline/     the ingestion pipeline (chunk → LLM audit → reconcile → review → apply)
test_data/    committed chunk snapshots produced by the pipeline (subset + full corpus)
```

## Quick start
```bash
cd pipeline
cp .env.example .env            # then fill in your USAi key + agency base URL (see below)

# parser only — no API key needed (chunks + manifest)
python pipeline.py run --no-llm

# full run with the LLM judge, restricted to the 8-section test set
python pipeline.py run --judge \
    --files 5.101 5.201 5.202 5.203 5.205 5.207 6.302-2 12.603
```
The pipeline reads its source from `../dita` (set in [`pipeline/pipeline.config.json`](pipeline/pipeline.config.json))
and writes working output to `pipeline/out/` (gitignored). Full docs:
[`pipeline/README.md`](pipeline/README.md).

## LLM provider — USAi.gov
USAi exposes an **OpenAI-compatible** Chat Completions API. Two settings are required for the live
LLM pass, both read from the environment / `pipeline/.env` (never committed):

| variable | meaning |
|----------|---------|
| `GEMINI_API_KEY` (or `USAI_API_KEY`) | your USAi API key |
| `USAI_BASE_URL` | your agency-specific endpoint, e.g. `https://<agency>.usai.gov` (shown in the USAi API console after login) |

`GEMINI_MODEL` selects the model id (USAi serves Gemini, Claude, GPT, Llama, Grok). See
[`pipeline/README.md`](pipeline/README.md) for every option.

## Notes
- **Secrets:** `.env` files are gitignored. Never commit your API key or database URLs.
- **`dita/` is source data**, lightly mixed with DITA build artifacts (`*.tmp`, `*.ditamap`,
  publish logs) that ship alongside the government download; the pipeline only reads the `*.dita`
  files.
- The FAR is U.S. public-domain regulatory text (acquisition.gov).
