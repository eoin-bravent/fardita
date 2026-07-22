#!/usr/bin/env python3
"""Stage 4: apply the ledger's corroborations + review/auto decisions DIRECTLY to the
versioned store's rows, then stamp refs_verified_from. The store is the single source of
truth for verified refs.

Ported from pipeline/pipeline.cmd_apply, operating on an in-memory (ledger, decisions) pair
and a chunker Store instead of reading <REG>_ledger.json / <REG>_decisions.json from disk.
Only the AUDITED units' rows are touched — the current rows normally, or the rows in force on
`as_of` in full-history mode; every other row/version in the store is left exactly as it was.

The refs_verified_from stamp preserves the pipeline precedence EXACTLY:
    last_seen_version  or  (stamp_version if != 'unknown' else source_version)
chunker rows always carry last_seen_version, so the stamp is that row's most-recent ingested
edition — and because cross_references/external_references live in CONTENT_FIELDS (not
HASH_FIELDS), an unchanged-text re-ingest keeps the verified refs + this stamp, while a text
change opens a fresh unstamped row (and the errata path pops refs_verified_from) so it re-queues.
"""
from chunker import extract_json as X                    # build_external_edge (human-corrected externals)
from chunker.references import reconcile


def _unit_of(citation):
    return citation.split("(")[0]


def apply_ledger(st, ledger, decisions, *, as_of=None, stamp_version="", audited_units=None):
    """Mutate `st` in place and persist. Returns a small stats dict.

      st          — a chunker.store.Store (its rows are mutated + saved)
      ledger      — the reconcile ledger (list of items, internal + external)
      decisions   — review / auto decisions (auto_decisions() output, or human review);
                    merged here, later entries winning per (unit, scope, target, alt|locator)
      as_of       — YYYY-MM-DD: apply to the rows in force on this date (full-history mode);
                    None -> the current rows
      stamp_version — edition label for refs_verified_from when a row lacks last_seen_version
                    (normally unused; last_seen_version wins)."""
    ledger = ledger or []
    int_conf, ext_conf, ext_index = {}, {}, {}            # corroborated sets + external-item index
    for it in ledger:
        if it.get("scope", "internal") == "external":
            ekey = (it["unit"], it["target"], it.get("locator", ""))
            ext_index[ekey] = it
            if it["status"] == "corroborated":
                ext_conf.setdefault(it["unit"], set()).add((it["target"], it.get("locator", "")))
        elif it["status"] == "corroborated":              # keyed by (target, alternate): base + each variant distinct
            int_conf.setdefault(it["unit"], set()).add((reconcile.norm_cit(it["target"]), it.get("alternate", "")))

    # merge decisions; later entries win per (unit, scope, target, alternate|locator)
    merged = {}
    for d in (decisions or []):
        sc = d.get("scope", "internal")
        if sc == "external":
            merged[(d["unit"], sc, d["target"], d.get("locator", ""))] = d
        else:                                             # internal: variant distinguishes base from each alternate
            merged[(d["unit"], sc, reconcile.norm_cit(d.get("target", "")), d.get("alternate", ""))] = d
    decisions = list(merged.values())
    int_dec = [d for d in decisions if d.get("scope", "internal") != "external"]
    ext_dec = [d for d in decisions if d.get("scope") == "external"]
    # reject removes a ref; manual replaces it with corrected citation(s) -> both drop the original
    int_replaced = {(d["unit"], reconcile.norm_cit(d["target"]), d.get("alternate", ""))
                    for d in int_dec if d["choice"] in ("reject", "manual")}
    ext_replaced = {(d["unit"], d["target"], d.get("locator", ""))
                    for d in ext_dec if d["choice"] in ("reject", "manual")}

    # scope: rows we TOUCH (tag ref statuses / add accepted refs) = units in the ledger or decisions,
    # PLUS every successfully-audited unit — so a unit audited with NO references is still finalized
    # (marked verified) instead of re-queued forever. rows we STAMP refs_verified_from on = the
    # successfully-audited + human-decided units only, so a unit whose LLM call FAILED (absent from
    # audited_units) is re-queued next run rather than falsely marked verified. audited_units=None
    # (older callers) preserves the original behavior (stamp every touched unit).
    ledger_dec_units = {it["unit"] for it in ledger} | {d["unit"] for d in decisions}
    touch = ledger_dec_units | set(audited_units or ())
    stamp_units = touch if audited_units is None else (set(audited_units) | {d["unit"] for d in decisions})
    base_rows = st.as_of(as_of) if as_of else st.current_rows()
    rows = [r for r in base_rows if _unit_of(r["citation"]) in touch]

    # 1) tag existing (parser) refs with status; drop any the human rejected/replaced
    removed = 0
    by_cit = {}
    for r in rows:
        by_cit[(r["citation"], r.get("alternate", ""))] = r    # flat alternate rows share the base citation
        if r.get("alternate"):                                 # alternate chunk: own refs are parser-only (not reconciled)
            for cr in r["cross_references"]:
                cr.setdefault("status", "parser_only")
            for cr in r.get("external_references", []):
                cr.setdefault("status", "parser_only")
            continue
        iconf = int_conf.get(r["citation"], set())
        kept = []
        for cr in r["cross_references"]:
            t, a = reconcile.norm_cit(cr["target"]), cr.get("alternate", "")
            if (r["citation"], t, a) in int_replaced:
                removed += 1
                continue
            cr["status"] = "corroborated" if (t, a) in iconf else "parser_only"   # parser already carries `alternate`
            kept.append(cr)
        r["cross_references"] = kept
        econf = ext_conf.get(r["citation"], set())
        ekept = []
        for cr in r.get("external_references", []):
            k = (cr["target"], cr.get("locator", ""))
            if (r["citation"], cr["target"], cr.get("locator", "")) in ext_replaced:
                removed += 1
                continue
            cr["status"] = "corroborated" if k in econf else "parser_only"
            ekept.append(cr)
        if "external_references" in r:
            r["external_references"] = ekept

    # 2) append human-approved / auto-accepted additions to their unit row
    added = 0
    for d in int_dec:                                     # internal: accepted llm-only/added + manual
        if d["choice"] == "manual":
            tgts = d.get("value", [])
        elif d["choice"] == "accept" and d.get("status") in ("llm_only", "added"):
            tgts = [d["target"]]
        else:
            continue
        u = by_cit.get((d["unit"], ""))                       # decisions are for base units (alternate '')
        if not u:
            continue
        acc_status = "auto_accepted" if d.get("by") == "auto" else "human_approved"
        evidence = "(auto-accept)" if d.get("by") == "auto" else "(human review)"
        a = d.get("alternate", "")                            # the variant this decision concerns ('' = base)
        for tgt in tgts:
            if any(reconcile.norm_cit(c["target"]) == reconcile.norm_cit(tgt)
                   and c.get("alternate", "") == a for c in u["cross_references"]):
                continue
            u["cross_references"].append({"target": tgt, "alternate": a, "confidence": "inferred",
                                          "mentions": [{"kind": "inferred", "evidence": evidence}],
                                          "status": acc_status})
            added += 1
    for d in ext_dec:                                     # external: accepted llm-only + manual corrections
        u = by_cit.get((d["unit"], ""))                       # decisions are for base units (alternate '')
        if not u:
            continue
        if d["choice"] == "accept" and d.get("status") == "llm_only":
            it = ext_index.get((d["unit"], d["target"], d.get("locator", "")))
            edges = [it] if it else []
        elif d["choice"] == "manual":
            ed = d.get("edit") or {}
            edges = [X.build_external_edge(ed.get("document", ""), ed.get("section", ""),
                                           ed.get("ref_type", "other"))] if ed.get("document") else []
        else:
            continue
        u.setdefault("external_references", [])
        acc_status = "auto_accepted" if d.get("by") == "auto" else "human_approved"
        evidence = "(auto-accept)" if d.get("by") == "auto" else "(human review)"
        for e in edges:
            if any(c["target"] == e["target"] and c.get("locator", "") == e.get("locator", "")
                   for c in u["external_references"]):
                continue
            u["external_references"].append({
                "target": e["target"], "ref_type": e["ref_type"], "locator": e.get("locator", ""),
                "node_label": e.get("node_label", e["target"]), "href": e.get("href", ""),
                "division_levels": e.get("division_levels", []), "citation": e.get("citation", ""),
                "mentions": [{"kind": "inferred", "evidence": evidence}],
                "status": acc_status})
            added += 1

    stamped = 0
    for r in rows:                                        # verified as of this edition (successful audits + decisions only)
        if _unit_of(r["citation"]) in stamp_units:
            r["refs_verified_from"] = (r.get("last_seen_version")
                                       or (stamp_version if stamp_version != "unknown"
                                           else r.get("source_version", "")))
            stamped += 1
    st.save()
    return {"rows": len(rows), "units": len(touch), "stamped": stamped, "added": added,
            "removed": removed, "decisions": len(decisions)}
