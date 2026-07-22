#!/usr/bin/env python3
"""Orchestrate one agency's reference pass: audit -> reconcile -> (judge) -> apply.

Ported from pipeline/pipeline.cmd_audit, simplified for the chunker store which is already
built (post-ingest). The LLM input is ALWAYS the store row's flattened `text`, so ONE path
covers every storage era — there is no DITA source tree and no tree-vs-store consistency
check (both are pre-ingest concerns the pipeline needed because it chunked+audited in one go).

Queue = the units whose selected rows lack `refs_verified_from` (or an explicit --files list).
Selected rows = the current rows by default, or the rows in force on `as_of` (full-history
mode: loop `as_of` over an agency's edition dates oldest-first to cover each distinct row
version exactly once — unchanged text keeps its stamp and drops out of the queue).
"""
import os
import json

from chunker import paths
from chunker.store import Store
from chunker.references import config as rcfg
from chunker.references import extract, judge as judge_mod, reconcile, apply as apply_mod
from chunker.llm import client


def _unit_of(citation):
    return citation.split("(")[0]


def run_references_history(agency, *, base=None, files=None, judge=None, auto_accept=True,
                           mock_llm=None, limit=None, provider=None, concurrency=None,
                           out_dir=None, progress=True):
    """Audit EVERY distinct historical version of each unit — not just the current text.

    Loops the agency's edition effective_dates oldest-first (plus a final current-rows pass as a
    catch-all for the tip / any undated edition), auditing the rows in force on each date that
    still lack refs_verified_from. SCD-2 makes this exact and non-wasteful: byte-identical text
    across editions is ONE row, audited once, and its refs apply to every edition it is in force;
    any text change opens a NEW row, audited when its edition is reached — so every distinct text
    version gets its own refs, and identical text is never re-audited. Returns a list of per-pass
    summaries."""
    st = Store(paths.store_dir(agency, base), agency)
    if not st.rows:
        return [{"agency": agency, "status": "empty-store"}]
    dates = sorted({e.get("effective_date") for e in st.editions if e.get("effective_date")})
    passes = dates + [None]                      # None = current rows (tip + undated-edition catch-all)
    if progress:
        n_versions = sum(1 for r in st.rows if r["type"] in ("section", "subsection")
                         and not r.get("alternate"))
        print(f"[{agency}] all-history: {len(dates)} edition date(s) + current pass; "
              f"{n_versions} base row-versions to cover")
    summaries = []
    for d in passes:
        s = run_references(agency, base=base, as_of=d, files=files, judge=judge,
                           auto_accept=auto_accept, mock_llm=mock_llm, limit=limit,
                           provider=provider, concurrency=concurrency, out_dir=out_dir,
                           progress=progress)
        summaries.append(s)
    audited = sum(s.get("units", 0) for s in summaries if isinstance(s, dict))
    if progress:
        print(f"[{agency}] all-history done: {audited} unit-version(s) audited across {len(passes)} pass(es)")
    return summaries


def build_temporal_index(base=None):
    """{agency -> {addr -> [(from, to), ...]}} across ALL built stores, for TEMPORAL cross-agency
    reference resolution — a reference is resolved against the corpus as it stood on the referencing
    row's edition date. addr = each row's bare citation AND its section root; the interval is the
    row's [effective_from, effective_to). Needs every store present (any agency can be a target)."""
    index = {}
    for ag in paths.agencies():
        sd = paths.store_dir(ag, base)
        if not os.path.exists(os.path.join(sd, "store.json")):
            continue
        try:
            st = Store(sd, ag)
        except Exception:
            continue
        m = {}
        for r in st.rows:
            bare = reconcile.norm_cit(reconcile.strip_agency(r["citation"]))
            iv = (r.get("effective_from"), r.get("effective_to"))
            for a in (bare, reconcile.section_root(bare)):
                m.setdefault(a, []).append(iv)
        index[ag] = m
    return index


def normalize_store(agency, base=None, index=None, progress=True):
    """Credential-free store cleanup (NO LLM): clean raw ditaot href targets + TEMPORALLY classify
    cross-regulation / companion refs (target_agency / target_kind) and tag validation over every
    base row of one agency, then save. The deterministic half of the reference pass; run after a
    re-ingest with a global temporal index (build_temporal_index) so any agency can target any other,
    resolved as of each row's edition. Returns {agency, changed}."""
    st = Store(paths.store_dir(agency, base), agency)
    if not st.rows:
        return {"agency": agency, "status": "empty-store", "changed": 0}
    changed = reconcile.normalize_rows(st.rows, agency, index)   # all versions; filters to base rows
    st.save()
    if progress:
        print(f"[{agency}] normalized {changed} target(s) across {len(st.rows)} rows -> {st.path}")
    return {"agency": agency, "rows": len(st.rows), "changed": changed}


def detect_renumbering(base=None):
    """Flag agencies whose citation set changes sharply between consecutive editions (a renumbering /
    restructuring). Per agency, the worst (lowest) Jaccard(citations at edition_i, edition_{i+1})
    across its history. Returns [(agency, jaccard, date0, date1, n0, n1)] sorted worst-first."""
    norm = lambda r: reconcile.norm_cit(reconcile.strip_agency(r["citation"]))
    out = []
    for ag in paths.agencies():
        sd = paths.store_dir(ag, base)
        if not os.path.exists(os.path.join(sd, "store.json")):
            continue
        try:
            st = Store(sd, ag)
        except Exception:
            continue
        dates = sorted({e["effective_date"] for e in st.editions if e.get("effective_date")})
        if len(dates) < 2:
            continue
        sets = [(d, {norm(r) for r in st.as_of(d)
                     if r["type"] in ("section", "subsection") and not r.get("alternate")})
                for d in dates]
        worst = None
        for (d0, s0), (d1, s1) in zip(sets, sets[1:]):
            if not (s0 or s1):
                continue
            j = len(s0 & s1) / max(1, len(s0 | s1))
            if worst is None or j < worst[0]:
                worst = (j, d0, d1, len(s0), len(s1))
        if worst:
            out.append((ag, *worst))
    out.sort(key=lambda x: x[1])
    return out


def _cost(tokens, pricing):
    inr, outr = pricing.get("input_per_1m", 0) or 0, pricing.get("output_per_1m", 0) or 0
    c = lambda d: round(d["prompt"] / 1e6 * inr + (d["thinking"] + d["output"]) / 1e6 * outr, 4)
    tot = tokens["total"]
    return {"currency": pricing.get("currency", "USD"), "rates_per_1m": {"input": inr, "output": outr},
            "audit": c(tokens["audit"]), "judge": c(tokens["judge"]), "total": c(tot)}


def run_references(agency, *, base=None, as_of=None, files=None, judge=None,
                   auto_accept=True, mock_llm=None, limit=None, provider=None,
                   concurrency=None, out_dir=None, progress=True):
    """Run the pass for one agency. Returns a summary dict. Writes <out>/references/{ledger,
    token_usage}.json and applies accepted refs to the store when auto_accept (default)."""
    sdir = paths.store_dir(agency, base)
    st = Store(sdir, agency)
    if not st.rows:
        return {"agency": agency, "status": "empty-store"}

    cfg = rcfg.build_cfg(agency, provider=provider, concurrency=concurrency, judge=judge)
    out = out_dir or os.path.join(sdir, "references")
    os.makedirs(out, exist_ok=True)
    cache_dir = os.path.join(out, "llm_cache")

    rows = st.as_of(as_of) if as_of else st.current_rows()
    if not rows:
        return {"agency": agency, "status": "no-rows-in-force", "as_of": as_of}

    # queue: units lacking verified refs, or an explicit --files list
    if files:
        stems = [os.path.basename(f) for f in files]
        stems = [s[:-5] if s.endswith(".dita") else s for s in stems]
        want = {s if s.startswith(f"{agency}-") else f"{agency}-{s}" for s in stems} | set(stems)
    else:
        want = {_unit_of(r["citation"]) for r in rows if not r.get("refs_verified_from")}

    # audit units: base sections/subsections only (alternates share the base citation), store text
    units = [(r["citation"], r["text"]) for r in rows
             if r["type"] in ("section", "subsection") and not r.get("alternate")
             and r["citation"] in want]
    if limit:
        units = units[:limit]
    if not units:
        return {"agency": agency, "status": "queue-empty", "verified_already": True}

    client.TRACKER.reset()
    if mock_llm:
        llm = json.load(open(mock_llm, encoding="utf-8"))
        audited_ok = {c for c, _ in units}                # mock stands in for a successful audit of every queued unit
    else:
        if progress:
            print(f"[{agency}] auditing {len(units)} unit(s) with {cfg['gemini']['model']} "
                  f"via {cfg['provider']} (concurrency={cfg['concurrency']})…")
        llm = extract.audit(units, cfg, cache_dir, progress=progress)
        audited_ok = {c for c, r in llm.items() if r is not None}   # exclude units whose LLM call FAILED (retry them)

    addr = reconcile.build_address_map(rows)              # whole agency: cross-file targets validate
    queue_units = {u for u, _ in units}
    rows_q = [r for r in rows if _unit_of(r["citation"]) in queue_units]
    # R4: clean raw ditaot href targets ('Subpart_1901_4_T48_...' -> 'subpart 1901.4') on the audited
    # rows BEFORE reconcile, so parser refs corroborate the LLM's clean citations (rows are live store
    # refs, so apply persists). Cross-agency + validation classification is the standalone
    # `--normalize-only` pass (it needs the global temporal index), not this per-agency LLM step.
    n_norm = reconcile.clean_targets(rows_q)
    ledger, stats = reconcile.reconcile(rows_q, llm, addr)

    # optional judge over internal disagreements
    if cfg["gemini"].get("judge") and not mock_llm:
        review_idx = [i for i, it in enumerate(ledger)
                      if it["needs_review"] and it.get("scope") == "internal"]
        if review_idx:
            from collections import defaultdict
            raw_by_cit = dict(units)
            by_unit = defaultdict(list)
            for i in review_idx:
                by_unit[ledger[i]["unit"]].append(i)
            jobs = [(ucit, raw_by_cit.get(ucit, ""),
                     [{"n": i, "target": ledger[i]["target"], "alternate": ledger[i].get("alternate", ""),
                       "source": "parser" if ledger[i]["status"] == "parser_inferred" else "llm",
                       "evidence": (ledger[i]["parser"] or ledger[i]["llm"] or {}).get("evidence", "")}
                      for i in idxs])
                    for ucit, idxs in by_unit.items()]
            if progress:
                print(f"[{agency}] judging {len(jobs)} unit(s) with disagreements…")
            recs_by_unit = judge_mod.judge_all(jobs, cfg, cache_dir, progress=progress)
            for i in review_idx:
                recs = recs_by_unit.get(ledger[i]["unit"], {})
                if i in recs:
                    ledger[i]["judge"] = {"choice": recs[i].get("choice"),
                                          "value": recs[i].get("value", []),
                                          "rationale": recs[i].get("rationale", "")}

    tokens = client.TRACKER.summary()
    summary = {"agency": agency, "provider": cfg["provider"], "model": cfg["gemini"]["model"],
               "as_of": as_of, "units": len(units), "status_counts": stats,
               "normalized_targets": n_norm,
               "needs_review": sum(1 for it in ledger if it["needs_review"]),
               "tokens": tokens, "cost": _cost(tokens, cfg["pricing"])}
    json.dump(ledger, open(os.path.join(out, "ledger.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    json.dump(summary, open(os.path.join(out, "token_usage.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    if auto_accept:
        judge_on = bool(cfg["gemini"].get("judge"))
        decisions = reconcile.auto_decisions(ledger, judge_on)
        applied = apply_mod.apply_ledger(st, ledger, decisions, as_of=as_of, audited_units=audited_ok)
        summary["applied"] = applied
        if progress:
            print(f"[{agency}] applied: {applied['rows']} rows / {applied['units']} units "
                  f"(+{applied['added']} accepted, -{applied['removed']} rejected) -> {st.path}")
    else:
        summary["applied"] = None
        if progress:
            print(f"[{agency}] ledger written ({len(ledger)} refs, {summary['needs_review']} "
                  f"need review); NOT applied (--no-auto-accept)")

    if progress:
        print(f"[{agency}] reconcile: {stats}")
    return summary
