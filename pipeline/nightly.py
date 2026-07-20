#!/usr/bin/env python3
"""Nightly updater: pull every GSA-Acquisition-* GitHub repo and merge its current
edition into the matching agency store. The GitHub DITA is the canon source going
forward; this is the multi-regulation generalization of `pipeline.py update`.

  python nightly.py [--agency FAR,DFARS] [--repos-dir ../repos] [--stores-dir ../stores]

Per agency: clone-or-pull -> read <REG>.ditamap rev -> skip if the commit is already
in the store's edition registry -> chunk (chunker.py, canon) -> merge_snapshot at the
rev's effective date (new FAC/revision -> new versions; same edition, changed text ->
errata, which clears refs_verified_from so the unit re-queues for LLM audit) -> verify.

Designed for a GitHub Action (see nightly.yml): exit code 0 = all agencies clean,
1 = at least one failure (the summary JSON says which)."""
import os
import re
import sys
import json
import time
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import archive_adapter as A
import chunker as CK
from store import Store
from update import parse_effective_date, fac_id

def sh(args, cwd=None, ok_fail=False):
    p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 and not ok_fail:
        raise RuntimeError(f"{' '.join(map(str,args))}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def clone_or_pull(url, dest):
    if os.path.isdir(os.path.join(dest, ".git")):
        sh(["git", "-C", dest, "pull", "--ff-only"])
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        sh(["git", "clone", "--depth", "1", url, dest])
    return sh(["git", "-C", dest, "rev-parse", "HEAD"])


def update_agency(agency, repo_url, repos_dir, stores_dir):
    out = {"agency": agency, "status": "ok"}
    dest = os.path.join(repos_dir, os.path.basename(repo_url))
    try:
        head = clone_or_pull(repo_url, dest)
    except Exception as e:
        return {**out, "status": "clone-failed", "error": str(e)[:200]}
    out["commit"] = head[:9]

    profile = A.ensure_profile(agency)
    maps = [p for p in (os.path.join(dest, "dita", profile["ditamap"]),
                        os.path.join(dest, profile["ditamap"])) if os.path.exists(p)]
    if not maps:
        import glob as g
        hits = g.glob(os.path.join(dest, "**", "*.ditamap"), recursive=True)
        maps = [h for h in hits if os.path.basename(h).upper().startswith(agency)] or hits
    if not maps:
        return {**out, "status": "no-ditamap"}
    mappath = maps[0]
    rev, _ = CK.parse_ditamap(mappath)
    eff = parse_effective_date(rev) or time.strftime("%Y-%m-%d")
    out["rev"], out["effective_date"] = rev, eff

    sdir = os.path.join(stores_dir, agency)
    os.makedirs(sdir, exist_ok=True)
    st = Store(sdir, agency)
    if any(e.get("source_commit") == head for e in st.editions):
        return {**out, "status": "up-to-date"}

    cfg = A.default_cfg(agency)
    cfg["input_dir"] = os.path.dirname(mappath)
    cfg["source_version"] = rev or f"{agency} {eff}"
    cfg["pipeline_version"] = "nightly"
    rows, manifest, _ = CK.run_chunker(cfg)
    if not rows:
        return {**out, "status": "no-rows"}
    stats = st.merge_snapshot(rows, eff, cfg["source_version"],
                              source="gsa-github", source_commit=head)
    problems = st.verify()
    st.save()
    out["stats"] = {k: v for k, v in stats.items()
                    if isinstance(v, int) and v and k not in
                    ("snapshot_rows", "identities_in_snapshot")}
    out["rows"] = len(st.rows)
    if problems:
        out["status"] = "INVARIANT-FAIL"
        out["problems"] = problems[:5]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agency", default="", help="comma list; default = all with a repo")
    ap.add_argument("--repos-dir", default=os.path.join(ROOT, "repos"))
    ap.add_argument("--stores-dir", default=os.path.join(ROOT, "stores"))
    ap.add_argument("--summary", default=os.path.join(ROOT, "stores", "nightly_summary.json"))
    args = ap.parse_args()

    reg = json.load(open(os.path.join(HERE, "data", "agencies.json"), encoding="utf-8"))
    want = [a.strip().upper() for a in args.agency.split(",") if a.strip()]
    results = []
    for agency, cfg in sorted(reg.items()):
        if agency.startswith("_") or not isinstance(cfg, dict) or not cfg.get("github"):
            continue
        if want and agency not in want:
            continue
        print(f"=== {agency}")
        r = update_agency(agency, cfg["github"], args.repos_dir, args.stores_dir)
        print("   ", json.dumps(r, default=str)[:220])
        results.append(r)
    os.makedirs(os.path.dirname(args.summary), exist_ok=True)
    json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results},
              open(args.summary, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    bad = [r for r in results if r["status"] not in ("ok", "up-to-date")]
    print(f"\n{len(results)} agencies: {len(results)-len(bad)} clean, {len(bad)} attention")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
