#!/usr/bin/env python3
"""Stage 2b (optional): the LLM judge over the reconcile disagreements.

Ported from pipeline/gemini_audit.run_judge + _judge_one. Given a unit's text and the atomic
citations only ONE method found (parser_inferred | llm_only), the model recommends
accept / manual / reject + a rationale — pre-filling the review page (and, under --auto-accept,
driving the applied decisions directly). External refs are never judged (the judge prompt is
regulation-internal); a failed judge call leaves that unit's refs unchanged.
"""
from chunker.llm import client
from chunker.references.prompts import JUDGE_SYSTEM, JUDGE_SCHEMA, PROMPT_VERSION, judge_user_text


def judge_all(jobs, cfg, cache_dir, progress=True):
    """jobs: [(unit_cit, raw_text, discrepancies)]; discrepancies: [{n, target, source, evidence,
    alternate?}]. Returns {unit_cit: {n: rec}}. Only units WITH discrepancies are sent."""
    jobs = [j for j in jobs if j[2]]
    batch = [{"key": u, "cache_name": "judge_" + u.replace("/", "_"),
              "system": JUDGE_SYSTEM.format(regulation=cfg["regulation"], citation=u),
              "user": raw + "\n\n" + judge_user_text(u, disc), "schema": JUDGE_SCHEMA}
             for u, raw, disc in jobs]
    out = client.run_batch(batch, cfg, cache_dir, stage="judge",
                           prompt_version=PROMPT_VERSION, progress=progress)
    return {u: {r["n"]: r for r in (recs or []) if isinstance(r, dict) and "n" in r}
            for u, recs in out.items()}
