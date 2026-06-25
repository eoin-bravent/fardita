# FAR cross-reference pipeline

Builds a verified map of every cross-reference in the **Federal Acquisition Regulation (FAR)** —
both **internal** (FAR → FAR) and **external** (FAR → U.S.C. / CFR / E.O. / Pub. L. / OMB). A
deterministic parser and an LLM each find the references, you resolve the disagreements in a browser,
and the result is a provenance-tagged dataset (nodes + edges) ready for a graph / RAG.

## Workflow — three steps

```bash
cd pipeline

# 1. RUN — chunk the FAR and find its references (parser + LLM)
python pipeline.py run --judge

# 2. REVIEW — open the page, accept / reject / fix the flagged items, click "Save & Apply"
python pipeline.py review

# 3. DONE — your final dataset is out/FAR_verified.json
```

**First time, try a handful of sections** so it's fast and cheap (a few cents):
```bash
python pipeline.py run --judge --files 5.101 5.202 5.203 6.302-2 --limit 5
python pipeline.py review
```

No API key yet? `python pipeline.py run --no-llm` chunks the FAR with the parser alone (no LLM).

## What you get
`out/FAR_verified.json` — every FAR unit with its cross-references, each tagged with **where it came
from** (`parser` / `parser+llm` / `llm+human` / `human`) and a **status** (`corroborated` /
`parser_only` / `human_approved`). Internal references and external ones (U.S.C./CFR/…) are kept in
separate lists.

## Setup — pick one LLM provider
Copy `pipeline/.env.example` to `pipeline/.env` and fill in **one** of these (`.env` is gitignored —
never commit a key):

**USAi.gov** (GSA's OpenAI-compatible AI gateway)
```
GEMINI_API_KEY=<your USAi key>
USAI_BASE_URL=https://<agency>.usai.gov
```

**Google Vertex AI** (Gemini direct — used on the GSA machine)
```
LLM_PROVIDER=vertex
GOOGLE_APPLICATION_CREDENTIALS=C:\path\to\service-account.json
```
then `pip install -r pipeline/requirements-vertex.txt`

Both default to `gemini-2.5-pro`.

## Layout
```
dita/         FAR DITA source (3,902 .dita files — the government download)
pipeline/     the pipeline + full documentation (pipeline/README.md)
test_data/    committed chunk snapshots
```

## Good to know
- **Reruns are cached** — only changed sections re-call the LLM, so the first full run is the only
  expensive one. The console (and the review page) show token usage and an estimated cost.
- **Full reference / every option:** [`pipeline/README.md`](pipeline/README.md).
- The FAR is U.S. public-domain regulatory text (acquisition.gov).
