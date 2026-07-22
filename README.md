# FARDITA — versioned acquisition-regulation corpus + cross-reference pipeline

Builds and maintains a **time-versioned store of U.S. acquisition regulations** (FAR and its
agency supplements — AFARS, DFARS, …) going back to the 1990s, and a **verified map of every
cross-reference** in them (internal FAR → FAR and external → U.S.C. / CFR / E.O. / Pub. L. / OMB).

Three ideas hold it together:

1. **One SCD-Type-2 store per regulation** — every section/paragraph is a row with
   `[effective_from, effective_to)`; ask for the text *as of any date*.
2. **Era parsers with proof** — acquisition.gov archives span five HTML generations (1996–today);
   each era has its own parser, and every ingested edition gets a *conservation-of-text audit*
   certifying that ≈100 % of the source characters are accounted for.
3. **LLM-assisted references** — a deterministic parser and an LLM independently find
   cross-references; disagreements go to a judge / browser review; verified edges survive
   store updates by design.

## Repository layout

| Path | What it is |
|---|---|
| `pipeline/` | **All the code.** One flat Python package — parsers, store, verification, LLM flow, and the app (orchestrator, dashboard, nightly updater). See `pipeline/README.md` for the file-by-file map and full usage. |
| `docs/` | Design notes, handoff docs, the query UI. |
| `.github/workflows/` | Nightly GitHub-repo sync action. |
| `stores/<REG>/` | *(local / LFS)* The versioned stores — one folder per regulation. |
| `archive_far/`, `archive_afars/`, … | *(local, gitignored)* Downloaded acquisition.gov archives. |
| `repos/` | *(local, gitignored)* Clones of the GSA source repos, managed by the app. |
| `dita/`, `test_data/` | *(local, gitignored)* Working DITA snapshot and sample outputs. |

Everything gitignored is re-downloadable or regenerable; the published repo is just code + docs.

## Quick start

```bash
cd pipeline
pip install -r requirements.txt

python dashboard.py                      # browser GUI: download → parse → verify, per agency
# or headless:
python orchestrator.py run --agency FAR --steps download,survey,backfill,audit
```

The LLM steps (`llm-triage`, `llm` reference extraction with judge/auto-accept) run separately
on a credentialed machine. LLM provider setup (`.env`, one key needed), the review browser,
and the full command reference all live in `pipeline/README.md`.

## Verification, in one line

For every edition of every regulation we publish an audit certificate: *N characters in the
source HTML, X % accounted for in the store or classified residue (nav/TOC/furniture), 0 %
unclassified.* The dashboard renders these per agency; `pipeline/corpus_audit.py` computes them.
