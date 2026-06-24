#!/usr/bin/env python3
"""Dry run: parse the whole DITA corpus in memory and report scale + size.
No database, no seed file written."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_to_sql as P

files = P.all_files()
fails, skips = P.run(files)

canon = sum(len(c[5]) for c in P.chunks)      # canonical_text chars
enr   = sum(len(c[6]) for c in P.chunks)      # enriched_text chars
mb = (canon + enr) / 1_000_000

print(f"files on disk:        {len(files)}")
print(f"parse failures:       {len(fails)}")
print(f"skipped (no concept): {len(skips)}")
print(f"content_items:        {len(P.items):,}")
print(f"chunks:               {len(P.chunks):,}")
print(f"relationships:        {len(P.rels):,}")
print(f"  medium (ambiguous): {sum(1 for r in P.rels if r[3]=='medium'):,}")
print(f"  low (ranges):       {sum(1 for r in P.rels if r[3]=='low'):,}")
print(f"  external:           {sum(1 for r in P.rels if r[3]=='external'):,}")
print(f"text payload:         ~{mb:.1f} MB (canonical+enriched, pre-index)")

# how many cross-ref targets actually resolve within the loaded corpus?
ids = {it[0] for it in P.items}
internal = [r for r in P.rels if r[1] is not None]
resolved = sum(1 for r in internal if r[1] in ids)
print(f"resolvable edges:     {resolved:,} / {len(internal):,} "
      f"({100*resolved/max(internal.__len__(),1):.0f}% of targeted refs land in-corpus)")

if fails[:5]:
    print("\nsample failures:")
    for f, e in fails[:5]:
        print(f"  {f}: {e[:90]}")
if skips[:10]:
    print(f"\nsample skipped: {', '.join(skips[:10])}")
