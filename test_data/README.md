# FAR chunk data

Committed snapshots produced by the ingestion pipeline (`_pgtest/pipeline/`). Both files share
the same schema; they differ only in scope.

| file | scope | rows |
|------|-------|------|
| [`FAR_chunks_subset.json`](FAR_chunks_subset.json) | curated 8-section test set (5.101, 5.201–5.207, 12.603, 6.302-2) | 45 |
| [`FAR_chunks_all.json`](FAR_chunks_all.json) | the whole FAR corpus | 11,230 |

**Schema, the full pipeline (chunk → LLM audit → human review → verified), and all options are
documented in [`../_pgtest/pipeline/README.md`](../_pgtest/pipeline/README.md)** — that is the single
source of truth. In short, each row carries `citation` (e.g. `FAR-5.203`), `regulation`, `type`,
bare FAR-decomposed identifiers, `url`, `text`, and `cross_references` (grouped by `target`, each
with `confidence` and `mentions[{kind, context}]`).

## Regenerate
The pipeline writes to its own `out/` directory (gitignored); these two files are the committed
copies of that output.
```
# subset
python _pgtest/pipeline/pipeline.py run --no-llm \
    --files 5.201 5.202 5.203 5.205 5.207 12.603 5.101 6.302-2
cp _pgtest/pipeline/out/FAR_chunks.json test_data/FAR_chunks_subset.json

# whole corpus
python _pgtest/pipeline/pipeline.py run --no-llm
cp _pgtest/pipeline/out/FAR_chunks.json test_data/FAR_chunks_all.json
```
