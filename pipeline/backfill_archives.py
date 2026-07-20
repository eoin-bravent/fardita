#!/usr/bin/env python3
"""Oldest-first backfill driver for the acquisition.gov archive editions.

Loops the archive editions of one or more eras in chronological order, and for each:
  chunk  ->  collapse-cosmetic (cross-source seam)  ->  merge_snapshot  ->  verify
keeping ONE Store in memory (loaded once, saved after each edition so the run is
crash-safe and resumable). Writes a combined collapse audit and a per-edition report.

Defaults to the webworks-2005 era (168 "FAC 2005-xx" folders, ~2005-2018), the largest
missing block. Effective dates come from archive_dates.json (authoritative).

Usage:
  # dry run: just print the plan (editions, dates) -- ingests nothing
  python backfill_archives.py --store-dir store --archives-dir ../archive_far --plan

  # real backfill into a COPY first (always validate before the live store)
  python backfill_archives.py --store-dir store_copy --archives-dir ../archive_far

  # options
  --eras webworks-2005            comma list of eras to include (default webworks-2005)
  --limit N                       only the first N editions (oldest N) -- for testing
  --since 2005-30 / --until 2005-99   FAC-number range filter
  --no-collapse                   ingest verbatim (no cosmetic collapse)
  --drop-quotes                   also treat quote-around-term diffs as cosmetic
  --hints-store-dir DIR           store to derive structure hints from (default: --store-dir)
  --stop-on-verify-fail           abort if invariants break after an edition (default on)
  --audit-out / --report-out      output paths

Resumable: editions whose folder is already recorded in the store's edition registry
(source_commit) are skipped, so re-running continues where it left off.
"""
import os
import sys
import json
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import archive_adapter as A
from store import Store


def _pipeline_rev():
    return A._pipeline_rev()


def load_plan(archives_dir, eras, since, until, limit, regulation="FAR"):
    """Editions to ingest, oldest-first: [{folder, path, era, fac, date, source_version}]."""
    eras_path = A.eras_path(regulation)
    classified = json.load(open(eras_path, encoding="utf-8")) if os.path.exists(eras_path) else {}
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store-dir", required=True)
    ap.add_argument("--archives-dir", default=os.path.join(HERE, "..", "archive_far"))
    ap.add_argument("--eras", default="webworks-2005",
                    help="comma list; FAR: legacy,webworks-2001,webworks-2005,ditaot -- "
                         "AFARS: transit,agov,ditaot,dita")
    ap.add_argument("--regulation", default="FAR")
    ap.add_argument("--partial-threshold", type=float, default=0.5, metavar="F",
                    help="a snapshot covering < F of the store's current units merges as "
                         "PARTIAL (complete=False: add/update only, nothing closed)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--plan", action="store_true", help="print the plan and exit (no ingest)")
    ap.add_argument("--no-collapse", action="store_true")
    ap.add_argument("--drop-quotes", action="store_true")
    ap.add_argument("--hints-store-dir", default="")
    ap.add_argument("--no-stop-on-verify-fail", action="store_true")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the automatic pre-run .bak snapshot of the store file")
    ap.add_argument("--save-every", type=int, default=1, metavar="N",
                    help="write the store to disk every N editions instead of every one "
                         "(the save is the dominant cost as the store grows; a crash loses "
                         "at most the last N-1 editions, and re-running resumes cleanly)")
    ap.add_argument("--verify-every", type=int, default=1, metavar="N",
                    help="run the full invariant check every N editions (always at the end)")
    ap.add_argument("--audit-out", default=os.path.join(HERE, "backfill_collapse_audit.json"))
    ap.add_argument("--report-out", default=os.path.join(HERE, "backfill_report.json"))
    args = ap.parse_args()

    eras = [e.strip() for e in args.eras.split(",") if e.strip()]
    plan = load_plan(args.archives_dir, eras, args.since, args.until, args.limit,
                     args.regulation)
    if not plan:
        sys.exit("no editions match the filters")

    print(f"backfill plan: {len(plan)} edition(s), {plan[0]['fac']} ({plan[0]['date']}) "
          f"-> {plan[-1]['fac']} ({plan[-1]['date']})  eras={eras}")
    if args.plan:
        for e in plan:
            print(f"  {e['date']}  {e['fac']:16} {e['folder']}")
        return

    store = Store(args.store_dir, args.regulation)
    done = {ed.get("source_commit") for ed in store.editions}
    print(f"store: {store.path}  rows={len(store.rows)}  editions={len(store.editions)}  "
          f"floor={min((e['effective_date'] for e in store.editions), default='-')}")

    # safety net: snapshot the store (+ errata log) before touching it, so a bad run is
    # reverted by copying the .bak back -- no separate working-copy ritual needed.
    if not args.no_backup and store.rows:
        import shutil
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for f in (store.path, store.errata_path):
            if os.path.exists(f):
                shutil.copy2(f, f + f".bak-{stamp}")
        print(f"backup written: {store.path}.bak-{stamp}")
        print(f"  (revert = copy the .bak back over "
              f"{os.path.basename(store.path)}; nothing else is modified)")

    # structure hints (row-labels + breadcrumb titles): derived ONCE from the store, used
    # by the webworks parser to reject FrameMaker-mis-rendered nested items. Stable across
    # the era, so one derivation at the store's floor is enough.
    hints_dir = args.hints_store_dir or args.store_dir
    floor = min((e["effective_date"] for e in store.editions), default="9999")
    print(f"deriving structure hints from {hints_dir} as of {floor} …", flush=True)
    hints = A.derive_hints(hints_dir, floor, args.regulation)

    cfg_base = A.default_cfg(args.regulation)
    cfg_base["pipeline_version"] = _pipeline_rev()

    audit = json.load(open(args.audit_out, encoding="utf-8")) \
        if os.path.exists(args.audit_out) else {}
    report = json.load(open(args.report_out, encoding="utf-8")) \
        if os.path.exists(args.report_out) else {"editions": []}

    n = len(plan)
    for i, e in enumerate(plan, 1):
        if e["folder"] in done:
            print(f"[{i}/{n}] {e['fac']:16} {e['date']}  -- already ingested, skip")
            continue
        t0 = time.time()
        chunker = A.ERA_CHUNKERS.get(e["era"])
        if chunker is None:
            print(f"[{i}/{n}] {e['fac']:16} -- no parser for era {e['era']}, skip")
            continue
        cfg = dict(cfg_base)
        cfg["source_version"] = e["source_version"]
        rows, manifest = chunker(e["path"], cfg, hints)

        collapsed = []
        if not args.no_collapse:
            rows, collapsed = A.collapse_cosmetic(rows, store, e["date"],
                                                  drop_quotes=args.drop_quotes)
            audit[e["folder"]] = {"fac": e["fac"], "date": e["date"],
                                  "collapsed_count": len(collapsed),
                                  "collapsed": [list(k) for k in collapsed]}

        # PARTIAL editions (some AFARS folders hold only the 1-3 changed parts): a
        # complete-mode merge would close every section they don't contain, so switch to
        # complete=False (add/update only) when the snapshot covers too little of the
        # store's current view.
        cur_units = {r["citation"] for r in store.current_rows()
                     if "(" not in r["citation"] and not r.get("alternate")}
        snap_units = {r["citation"] for r in rows
                      if "(" not in r["citation"] and not r.get("alternate")}
        complete = (not cur_units
                    or len(snap_units) >= args.partial_threshold * len(cur_units))
        stats = store.merge_snapshot(rows, e["date"], e["source_version"],
                                     source=A.SOURCE, source_commit=e["folder"],
                                     complete=complete)
        is_last = (i == n)
        problems = store.verify() if (i % args.verify_every == 0 or is_last) else None
        if i % args.save_every == 0 or is_last or problems:
            store.save()

        dt = time.time() - t0
        s = stats
        vtxt = "skip" if problems is None else ("OK" if not problems else "FAIL")
        print(f"[{i}/{n}] {e['fac']:16} {e['date']}  "
              f"chunks={len(rows)} collapsed={len(collapsed)} "
              f"new={s['new']} changed={s['changed']} backfilled={s['backfilled']} "
              f"extended_back={s['extended_backward']} closed={s['closed']} "
              f"gap_split={s['gap_split']}{'' if complete else '  PARTIAL'}  "
              f"verify={vtxt}  {dt:.0f}s", flush=True)

        report["editions"].append({
            "folder": e["folder"], "fac": e["fac"], "date": e["date"],
            "era": e["era"], "chunks": len(rows), "collapsed": len(collapsed),
            "complete": complete,
            "stats": {k: v for k, v in stats.items() if k != "sections_changed"},
            "verify_ok": (None if problems is None else not problems),
            "seconds": round(dt, 1)})
        json.dump(audit, open(args.audit_out, "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)
        json.dump(report, open(args.report_out, "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)

        if problems and not args.no_stop_on_verify_fail:
            print(f"  INVARIANT FAILURE after {e['fac']} -- stopping. First problems:")
            for p in problems[:10]:
                print(f"    ! {p}")
            sys.exit(1)

    total_collapsed = sum(v["collapsed_count"] for v in audit.values())
    print(f"\ndone: store rows={len(store.rows)} editions={len(store.editions)} "
          f"floor={min(e['effective_date'] for e in store.editions)}")
    print(f"combined collapse: {total_collapsed} chunks across {len(audit)} editions "
          f"-> {args.audit_out}")
    print(f"per-edition report -> {args.report_out}")
    problems = store.verify()
    print(f"final invariants: {'OK' if not problems else f'{len(problems)} PROBLEM(S)'}")


if __name__ == "__main__":
    main()
