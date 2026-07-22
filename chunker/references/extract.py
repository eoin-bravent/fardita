#!/usr/bin/env python3
"""Stage 2a: the blind LLM audit over a unit's own text.

Ported from pipeline/gemini_audit.run_audit, but ERA-AGNOSTIC: the LLM input is the store
row's flattened `text` (the same field for a GitHub-DITA unit and an archive-HTML unit alike),
not raw per-era source. That is the key simplification that lets one audit path cover all
storage eras (REFERENCE_ENGINE_EXTENSION §6 cost answer): audit each unit's current text once;
unchanged historical rows share that text (and its verified refs) via the store's content_hash.

The prompt + schema are chunker.references.prompts (task logic); the threading / caching /
token tracking is the task-agnostic chunker.llm.client.run_batch.
"""
from chunker.llm import client
from chunker.references.prompts import AUDIT_SYSTEM, AUDIT_SCHEMA, PROMPT_VERSION


def _coerce(res):
    """Keep the cache clean: an audit result must be a list of ref dicts."""
    return [r for r in res if isinstance(r, dict)] if isinstance(res, list) else []


def audit(units, cfg, cache_dir, progress=True):
    """units: [(citation, text)]. Returns {citation: [ {target, evidence, scope, ...} ]}.
    A unit whose LLM call fails contributes no LLM refs (its parser refs are unaffected) —
    its cache entry is left unwritten, so a later re-run retries just that unit."""
    jobs = [{"key": cit, "cache_name": cit.replace("/", "_"),
             "system": AUDIT_SYSTEM.format(regulation=cfg["regulation"], citation=cit),
             "user": text, "schema": AUDIT_SCHEMA, "coerce": _coerce}
            for cit, text in units]
    out = client.run_batch(jobs, cfg, cache_dir, stage="audit",
                           prompt_version=PROMPT_VERSION, progress=progress)
    # Keep None for a FAILED call (distinct from a genuine empty [] result): the caller stamps
    # refs_verified_from only on successfully-audited units, so a failed unit retries next run.
    # reconcile coerces None -> [] when building the ledger, so downstream is unaffected.
    return {cit: res for cit, res in out.items()}
