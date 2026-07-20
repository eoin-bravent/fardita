#!/usr/bin/env python3
"""Verify the versioned chunk store: invariants, summary, and as-of spot checks.

    python verify_store.py [--store-dir DIR] [--regulation FAR]
                           [--expect "CITATION|DATE|SUBSTRING"]...
                           [--as-of "CITATION|DATE"]...

Invariants (per (citation, alternate) chain):
    * intervals well-formed (effective_from < effective_to when closed)
    * no overlaps; at most one open row and it is last; no duplicate effective_from
    * current == (effective_to is None)
Summary: editions ingested, row/identity counts, in-force count at each edition date.
--expect asserts a substring appears in the text of the row in force for CITATION on DATE
(exit code 1 on any failure or invariant problem).  --as-of just prints the row's text head.
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import Store


def find(store, citation, date):
    rows = store.as_of(date, citation=citation)
    base = [r for r in rows if not r.get("alternate")]
    return base[0] if base else (rows[0] if rows else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store-dir", default=os.path.join(HERE, "store"))
    ap.add_argument("--regulation", default="FAR")
    ap.add_argument("--expect", action="append", default=[],
                    metavar='"CITATION|DATE|SUBSTRING"')
    ap.add_argument("--as-of", action="append", default=[], dest="asof",
                    metavar='"CITATION|DATE"')
    args = ap.parse_args()

    store = Store(args.store_dir, args.regulation)
    if not store.rows:
        sys.exit(f"store is empty: {store.path}")
    failures = 0

    # ---- invariants ----
    problems = store.verify()
    print(f"== invariants: {'OK' if not problems else f'{len(problems)} PROBLEM(S)'} ==")
    for p in problems[:50]:
        print(f"  ! {p}")
    failures += len(problems)

    # ---- summary ----
    chains = store.chains()
    cur = store.current_rows()
    multi = sum(1 for c in chains.values() if len(c) > 1)
    print(f"\n== summary ==")
    print(f"rows: {len(store.rows)}   identities: {len(chains)}   "
          f"multi-version chains: {multi}   current rows: {len(cur)}")
    print(f"editions ({len(store.editions)}):")
    for e in store.editions:
        s = e.get("stats", {})
        n_force = len(store.as_of(e["effective_date"]))
        print(f"  {e['effective_date']}  {e['source_version'][:44]:<44} "
              f"in-force={n_force}  new={s.get('new', 0)} changed={s.get('changed', 0)} "
              f"closed={s.get('closed', 0)} errata={s.get('errata_replaced', 0)}"
              f"+{s.get('errata_removed', 0)}")

    # consistency: every chain has at most one current row; current == in force at max date
    open_per_chain = sum(1 for c in chains.values()
                         if sum(1 for r in c if r["effective_to"] is None) > 1)
    if open_per_chain:
        print(f"  ! {open_per_chain} chains with >1 open row")
        failures += open_per_chain

    # ---- errata / changelog ----
    epath = store.errata_path
    if os.path.exists(epath):
        errata = json.load(open(epath, encoding="utf-8"))
        acts = {}
        for it in errata:
            acts[it["action"]] = acts.get(it["action"], 0) + 1
        print(f"\nerrata log: {len(errata)} entries {acts}")
        cits = sorted({it['row']['citation'] for it in errata})
        print(f"  citations: {', '.join(cits[:12])}{' …' if len(cits) > 12 else ''}")
    cpath = os.path.join(store.dir, f"{args.regulation}_changelog.json")
    if os.path.exists(cpath):
        acc = json.load(open(cpath, encoding="utf-8"))
        print(f"changelog: {len(acc)} edition(s)")
        for k, v in sorted(acc.items(), key=lambda kv: kv[1]["effective_date"]):
            print(f"  {v['effective_date']}  {k[:44]:<44} {len(v['entries'])} LSA entries")

    # ---- spot checks ----
    if args.asof or args.expect:
        print(f"\n== spot checks ==")
    for spec in args.asof:
        cit, date = [x.strip() for x in spec.split("|")]
        r = find(store, cit, date)
        if r is None:
            print(f"  as-of {cit} @ {date}: NOT IN FORCE")
        else:
            print(f"  as-of {cit} @ {date}: [{r['effective_from']},"
                  f"{r['effective_to'] or 'open'}) {r['source_version'][:24]}: "
                  f"{r['text'][:110]!r}")
    for spec in args.expect:
        cit, date, sub = [x.strip() for x in spec.split("|", 2)]
        r = find(store, cit, date)
        ok = r is not None and sub in r["text"]
        print(f"  {'PASS' if ok else 'FAIL'}  {cit} @ {date} contains {sub!r}"
              + ("" if r is None else f"  [{r['effective_from']},{r['effective_to'] or 'open'})"))
        if not ok:
            failures += 1
            if r is not None:
                print(f"        text head: {r['text'][:160]!r}")

    print(f"\n{'ALL OK' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
