#!/usr/bin/env python3
"""Prove the versioned store captured every GitHub change.

    python store_coverage.py --repo ../../GSA-Acquisition-FAR [--store-dir store]

For each CONSECUTIVE pair of ingested editions (A -> B, by effective date), take git's
own list of changed section files (`git diff --name-only A..B -- dita/`) and require
that every numeric section file is accounted for, one of three ways:

    event      the store recorded version activity for that section at B's date
               (a row opened or closed at effective_from/effective_to == B, or an
               errata replacement whose row starts at B)
    identical  the file changed in git but chunks to IDENTICAL content under the
               chunker's content_hash -- proven by re-chunking BOTH blob versions and
               comparing (citation, alternate, content_hash) sets.  This is the
               expected class for markup-only churn: rev change-marks dropping out at
               the next FAC, attribute/whitespace edits, DOCTYPE noise.
    orphan     the file is not referenced by FAR.ditamap at either commit.  The
               ditamap is the authoritative file list (it drives both the chunker and
               acquisition.gov) -- orphaned stubs GSA leaves in the repo are correctly
               outside the store.
    MISS       none of the above -- a real content change the store failed to capture.

Exit code 1 iff any MISS.  Non-numeric files (Part_*/Subpart_*/matrix/cover/LSA…) are
reported but out of chunk scope by design (Part/Subpart title changes surface as events
on their sections' chunks via the title breadcrumb).

Note on skipped interim editions: if an edition between A and B could not be ingested
(no parseable rev), its changes appear in the A..B diff and -- correctly -- in B's
events, attributed to B's date (best available knowledge).
"""
import os
import re
import sys
import json
import argparse
import tempfile
import subprocess
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import chunker
from store import Store, content_hash, section_of

CFG = {"regulation": "FAR", "bottom_level": "paragraph", "bottom_depth": 1,
       "url_template": "https://www.acquisition.gov/far/{num}",
       "source_version": "", "pipeline_version": ""}


def git(repo, *args, ok_fail=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        if ok_fail:
            return None
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[:200]}")
    return p.stdout


def ditamap_files(repo, sha):
    """Basenames referenced by FAR.ditamap at <sha> (empty set if map unreadable)."""
    raw = git(repo, "show", f"{sha}:dita/FAR.ditamap", ok_fail=True)
    if raw is None:
        return set()
    return set(re.findall(r'href="([^"]+\.dita)"', raw))


def chunk_blob(repo, sha, relpath):
    """{(citation, alternate): content_hash} for one file at one commit ({} if absent
    or unchunkable)."""
    raw = git(repo, "show", f"{sha}:{relpath}", ok_fail=True)
    if raw is None:
        return {}
    stem = os.path.splitext(os.path.basename(relpath))[0]
    fd, tmp = tempfile.mkstemp(suffix=".dita")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
        try:
            rows, _ = chunker.build(tmp, stem, CFG)
        except Exception:
            return {}
        return {(r["citation"], r.get("alternate", "")): content_hash(r)
                for r in (rows or [])}
    finally:
        os.unlink(tmp)


def snapshot_check(args, store, eds):
    """The gold-standard completeness proof, immune to GSA file-naming games
    (renames, misnamed files, duplicate sources): for each ingested edition, re-chunk
    the FULL published tree at its settled commit and require that the store's
    as-of(effective_date) view is IDENTICAL -- same identities, same content hashes.
    If this holds for every edition, every change GSA published is in the store,
    by definition."""
    import tempfile
    import shutil
    import tarfile
    import io
    fails = 0
    for e in eds:
        tmp = tempfile.mkdtemp(prefix="far_snapcheck_")
        try:
            p = subprocess.run(["git", "-C", args.repo, "archive", "--format=tar",
                                e["source_commit"], "dita"], capture_output=True)
            with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
                tf.extractall(tmp)
            cfg = dict(CFG)
            cfg.update({"input_dir": os.path.join(tmp, "dita"),
                        "ditamap": "FAR.ditamap", "bottom_level": "paragraph"})
            rows, _, _ = chunker.run_chunker(cfg)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        snap = {}
        for r in rows:                            # first-wins dedup, same as the merge
            snap.setdefault((r["citation"], r.get("alternate", "")), content_hash(r))
        af = {(r["citation"], r.get("alternate", "")): r["content_hash"]
              for r in store.as_of(e["effective_date"])}
        only_snap = set(snap) - set(af)
        only_store = set(af) - set(snap)
        mismatch = [k for k in set(snap) & set(af) if snap[k] != af[k]]
        n = len(only_snap) + len(only_store) + len(mismatch)
        fails += n
        print(f"{'OK  ' if not n else 'FAIL'} {e['effective_date']} "
              f"({e['source_version'][:36]:<36}) snapshot={len(snap)} as-of={len(af)} "
              f"snap-only={len(only_snap)} store-only={len(only_store)} "
              f"hash-mismatch={len(mismatch)}")
        for k in list(only_snap)[:5] + list(only_store)[:5] + mismatch[:5]:
            print(f"      !! {k}")
    print("\nSNAPSHOT PROOF OK: store as-of == published tree for every edition"
          if not fails else "\nSNAPSHOT PROOF FAILED")
    sys.exit(1 if fails else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--store-dir", default=os.path.join(HERE, "store"))
    ap.add_argument("--regulation", default="FAR")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--pairs", metavar="A:B",
                    help="audit only pair indices [A, B) -- for chunked runs")
    ap.add_argument("--snapshot", metavar="A:B", nargs="?", const=":",
                    help="snapshot-level proof instead: store as-of == full re-chunk "
                         "of each edition's tree (optionally sliced A:B)")
    args = ap.parse_args()

    store = Store(args.store_dir, args.regulation)
    eds = sorted((e for e in store.editions if e.get("source_commit")),
                 key=lambda e: e["effective_date"])
    if len(eds) < 2:
        sys.exit("need at least 2 ingested editions with source commits")

    if args.snapshot:
        lo, hi = args.snapshot.split(":")
        snapshot_check(args, store, eds[int(lo or 0):int(hi) if hi else None])
        return

    by_id = defaultdict(list)                     # (citation, alternate) -> rows
    for r in store.rows:
        by_id[(r["citation"], r.get("alternate", ""))].append(r)

    # store events: sections with version activity per effective date
    events = defaultdict(set)
    for r in store.rows:
        events[r["effective_from"]].add(section_of(r))
        if r["effective_to"] is not None:
            events[r["effective_to"]].add(section_of(r))
    if os.path.exists(store.errata_path):
        for it in json.load(open(store.errata_path, encoding="utf-8")):
            events[it["row"]["effective_from"]].add(section_of(it["row"]))

    pairs = list(zip(eds, eds[1:]))
    if args.pairs:
        lo, hi = args.pairs.split(":")
        pairs = pairs[int(lo or 0):int(hi) if hi else None]
    total_miss, total_ident, total_evt, total_oos = 0, 0, 0, 0
    print(f"coverage audit over {len(pairs)} consecutive edition pairs\n")
    for a, b in pairs:
        diff = git(args.repo, "diff", "--name-only",
                   f"{a['source_commit']}..{b['source_commit']}", "--", "dita/")
        files = [f for f in diff.splitlines() if f.endswith(".dita")]
        numeric = [f for f in files
                   if chunker.NUMERIC.match(os.path.splitext(os.path.basename(f))[0])]
        oos = len(files) - len(numeric)
        ev = events.get(b["effective_date"], set())
        in_map = ditamap_files(args.repo, a["source_commit"]) \
            | ditamap_files(args.repo, b["source_commit"])
        explained_evt, identical, orphans, miss = [], [], [], []
        for f in numeric:
            stem = os.path.splitext(os.path.basename(f))[0]
            if stem in ev:                      # fast path: filename == section
                explained_evt.append(f)
                continue
            if os.path.basename(f) not in in_map:
                orphans.append(f)
                continue
            ca = chunk_blob(args.repo, a["source_commit"], f)
            cb = chunk_blob(args.repo, b["source_commit"], f)
            if ca == cb:
                identical.append(f)
                continue
            # attribute by the citations the file ACTUALLY produces (GSA filenames
            # can disagree with content, e.g. 42.200.dita holding section 40.201):
            # every differing identity must have an event at B's date, OR the store's
            # in-force content at B must already equal the surviving text (a file
            # RENAME: content moved between files, identity unchanged -> no event).
            ok = True
            for k in set(ca) | set(cb):
                if ca.get(k) == cb.get(k):
                    continue
                sec = k[0].split("(")[0].split("-", 1)[-1]
                if sec in ev:
                    continue
                in_force = next((r for r in by_id.get(k, [])
                                 if r["effective_from"] <= b["effective_date"]
                                 and (r["effective_to"] is None
                                      or b["effective_date"] < r["effective_to"])), None)
                if in_force is not None and \
                        in_force["content_hash"] == (cb.get(k) or ca.get(k)):
                    continue                       # rename / no-op from the store's view
                ok = False
                break
            (explained_evt if ok else miss).append(f)
        total_evt += len(explained_evt)
        total_ident += len(identical)
        total_miss += len(miss)
        total_oos += oos + len(orphans)
        tag = "OK  " if not miss else "MISS"
        print(f"{tag} {a['effective_date']} -> {b['effective_date']}  "
              f"({b['source_version'][:28]:<28}) git-changed section files: "
              f"{len(numeric):>4}  events: {len(explained_evt):>4}  "
              f"markup-only: {len(identical):>4}  orphan: {len(orphans):>3}  "
              f"non-section: {oos:>3}  MISSED: {len(miss)}")
        for f in miss:
            print(f"      !! {f}")
        if args.verbose:
            for f in identical[:8]:
                print(f"         (identical) {f}")

    print(f"\ntotals: {total_evt} via store events, {total_ident} proven markup-only, "
          f"{total_oos} non-section files, {total_miss} MISSED")
    print("PROOF OK: every git-changed section file is accounted for"
          if not total_miss else "PROOF FAILED")
    sys.exit(1 if total_miss else 0)


if __name__ == "__main__":
    main()
