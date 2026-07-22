"""Archive backfill: acquisition.gov historical editions -> the store, oldest-first.

Faithful port of pipeline/backfill_archives.py's discovery + merge loop:
  discover editions (era + authoritative date) -> chunk with the era parser ->
  collapse-cosmetic -> merge_snapshot -> verify, saving periodically (resumable).
Dropped vs the original: the CLI, and the timestamped .bak safety net (Decision D — the
clean rebuild writes into staging and swaps, and Store.save keeps one store.prev.json).
`A` is chunker.parsers._adapter, used exactly as backfill_archives.py used archive_adapter.
"""
import os
from chunker.parsers import _adapter as A
from chunker.ingest import ingest_edition
from chunker import paths

SOURCE = A.SOURCE                       # "acquisition-gov-archive"

# HTML archive eras (the "dita" zip era is ingested first, like the orchestrator does)
HTML_ERAS = ["legacy", "webworks-2001", "webworks-2005", "webworks-parts",
             "transit", "transit-farsite", "agov", "ditaot", "ditaot-topics"]
ALL_ERAS = ["dita"] + HTML_ERAS


def plan_editions(archives_dir, eras, regulation, since="", until="", limit=0):
    """Oldest-first [{folder, path, era, fac, date, source_version}] (port of load_plan)."""
    ep = A.eras_path(regulation)
    classified = _load(ep) if os.path.exists(ep) else {}
    profile = A.ensure_profile(regulation)
    label = "FAC" if regulation == "FAR" else regulation
    dpath = profile.get("dates", "")
    if not dpath or not os.path.exists(dpath):
        sib = os.path.join(archives_dir, "archive_metadata.json")
        dpath = sib if os.path.exists(sib) else dpath
    dates = A.load_archive_dates(dpath) if dpath else {}
    plan = []
    for folder in sorted(os.listdir(archives_dir)):
        path = os.path.join(archives_dir, folder)
        if not os.path.isdir(path):
            continue
        era = (classified.get(folder) or {}).get("era") or A.classify_folder(path)[0]
        if era not in eras:
            continue
        m = dates.get(folder)
        if not m or m.get("effective_date", "") <= "1980":     # undated / epoch placeholder
            continue
        num, date = m["number"], m["effective_date"]
        if since and num < since:
            continue
        if until and num > until:
            continue
        plan.append({"folder": folder, "path": path, "era": era, "fac": f"{label} {num}",
                     "date": date, "source_version": A._source_version(num, date, label)})
    plan.sort(key=lambda e: (e["date"], e["folder"]))          # oldest-first
    if limit:
        plan = plan[:limit]
    return plan


def backfill(store, regulation, archives_dir, eras=None, *, collapse=True, drop_quotes=False,
             partial_threshold=0.5, since="", until="", limit=0, hints=None,
             verify_every=10, save_every=10, on_edition=None):
    """Ingest the archive editions of `eras` into an already-loaded `store`, oldest-first.
    Returns {editions:[...], collapse_audit:{...}}. Resumable (skips editions already in the
    store that left live rows; re-ingests phantom editions)."""
    eras = eras if eras is not None else ALL_ERAS
    plan = plan_editions(archives_dir, eras, regulation, since, until, limit)
    report = {"editions": [], "collapse_audit": {}}
    if not plan:
        report["note"] = "no editions match the filters"
        return report

    done = {ed.get("source_commit") for ed in store.editions}
    alive = set()
    for r in store.rows:
        alive.add(r.get("source_version"))
        alive.add(r.get("last_seen_version"))
    alive.discard(None)
    alive.discard("")

    if hints is None:
        floor = min((e["effective_date"] for e in store.editions), default="9999")
        hints = A.derive_hints(store.dir, floor, regulation)

    cfg_base = A.default_cfg(regulation)
    cfg_base["pipeline_version"] = paths.pipeline_rev()

    audit = report["collapse_audit"]
    n = len(plan)
    unsaved = 0
    for i, e in enumerate(plan, 1):
        if e["folder"] in done:
            if e["source_version"] in alive:
                continue
            # phantom edition (recorded, no live rows) -> drop registry entry, re-ingest
            store.editions[:] = [x for x in store.editions
                                 if x.get("source_commit") != e["folder"]]
        chunker = A.ERA_CHUNKERS.get(e["era"])
        if chunker is None:
            continue
        cfg = dict(cfg_base)
        cfg["source_version"] = e["source_version"]
        rows, _manifest = chunker(e["path"], cfg, hints)
        if not rows:
            # zero-row guard: era mismatch (e.g. a FARSite mirror) -> refuse (no phantom)
            report["editions"].append({"folder": e["folder"], "fac": e["fac"],
                                       "date": e["date"], "era": e["era"],
                                       "chunks": 0, "skipped": "zero rows"})
            continue
        stats, collapsed = ingest_edition(
            store, rows, e["date"], e["source_version"], source=SOURCE,
            source_commit=e["folder"], complete=None, collapse=collapse,
            drop_quotes=drop_quotes, partial_threshold=partial_threshold)
        audit[e["folder"]] = {"fac": e["fac"], "date": e["date"],
                              "collapsed_count": len(collapsed),
                              "collapsed": [list(k) for k in collapsed]}
        unsaved += 1
        is_last = (i == n)
        problems = store.verify() if (i % verify_every == 0 or is_last) else None
        if i % save_every == 0 or is_last or problems:
            store.save()
            unsaved = 0
        report["editions"].append({
            "folder": e["folder"], "fac": e["fac"], "date": e["date"], "era": e["era"],
            "chunks": len(rows), "collapsed": len(collapsed),
            "stats": {k: v for k, v in stats.items() if k != "sections_changed"},
            "verify_ok": (None if problems is None else not problems)})
        if on_edition:
            on_edition(i, n, e, stats, collapsed, problems)
        if problems:
            report["invariant_failure"] = {"after": e["fac"], "problems": problems[:10]}
            break

    if unsaved:
        store.save()
    return report


def _load(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)
