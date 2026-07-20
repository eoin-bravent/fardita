#!/usr/bin/env python3
"""Scheduled-update + historical-replay drivers for the versioned store (no LLM).

    python pipeline.py update  [--repo R] [--effective-date YYYY-MM-DD] [--force]
        Ingest the CURRENT state of the GSA clone (cfg input_dir) into the store:
        detect new-edition vs errata from the ditamap rev, chunk the full file set,
        merge_snapshot, accumulate the LSA changelog, validate against the LSA,
        write a report, remember the processed commit in store/state.json.

    python pipeline.py replay  --repo R [--since 2025-04] [--errata-check] ...
        Walk the GSA repo's first-parent history, pick the SETTLED (last) commit of
        each FAC edition, and feed each through the same ingest path in order.
        --errata-check additionally ingests an earlier commit of the final edition
        first, so the settled pass exercises the errata (replace-in-place) path.

Both are deterministic: chunker + LSA parser only.  The GitHub clone is one adapter;
archive/manual adapters later feed the same Store.merge_snapshot().
"""
import io
import os
import re
import sys
import json
import shutil
import tarfile
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chunker
import changelog
from store import Store, utcnow

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DITAMAP_REL = "dita/FAR.ditamap"          # path inside the GSA repo
GITHUB_SOURCE = "gsa-github"


# ---------- small helpers ----------
def _git(repo, *args, ok_fail=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        if ok_fail:
            return None
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def _pipeline_rev():
    out = _git(HERE, "rev-parse", "--short", "HEAD", ok_fail=True)
    return out.strip() if out else "unknown"


def parse_effective_date(rev):
    """'FAC 2026-01 March 13, 2026' -> '2026-03-13'; None when unparseable ('August XX')."""
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", rev or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"


def fac_id(rev):
    """'FAC 2026-01 March 13, 2026' -> '2026-01'; None when absent."""
    m = re.search(r"FAC\s+(\d{4}-\d+)", rev or "")
    return m.group(1) if m else None


def rev_at_commit(repo, sha):
    raw = _git(repo, "show", f"{sha}:{DITAMAP_REL}", ok_fail=True)
    if raw is None:
        return None
    m = re.search(r'<map\b[^>]*\brev="([^"]*)"', raw)
    return m.group(1).strip() if m else None


def export_dita(repo, sha, dest):
    """Extract <sha>'s dita/ tree into dest/ (portable: tar via Python, no shell pipe)."""
    p = subprocess.run(["git", "-C", repo, "archive", "--format=tar", sha, "dita"],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git archive {sha} failed: "
                           f"{p.stderr.decode('utf-8', 'replace').strip()[:300]}")
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
        tf.extractall(dest)
    return os.path.join(dest, "dita")


# ---------- changelog accumulation + LSA validation ----------
def accumulate_changelog(store, entries, source_version, effective_date):
    """Append this edition's LSA entries to store/<REG>_changelog.json, keyed by
    source_version -- the persistent 'what did each FAC touch' index."""
    path = os.path.join(store.dir, f"{store.regulation}_changelog.json")
    acc = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    acc[source_version] = {"effective_date": effective_date, "ingested_at": utcnow(),
                           "entries": entries}
    tmp = path + ".tmp"
    json.dump(acc, open(tmp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return len(entries)


def lsa_compare(sections_changed, entries):
    """Two-way diff between what the merge detected and what GSA's LSA says this FAC
    amended.  Perfect agreement isn't expected (the LSA also lists parts/appendix/matrix
    rows the chunker skips; title cascades touch chunks the LSA doesn't list) -- this is
    a discrepancy REPORT, not an assertion."""
    lsa = {e["section"] for e in entries if e.get("section")}
    det = set(sections_changed)
    return {"lsa_sections": sorted(lsa), "detected_sections": sorted(det),
            "lsa_not_detected": sorted(lsa - det),
            "detected_not_in_lsa": sorted(det - lsa),
            "agreement": len(lsa & det)}


# ---------- one edition, one ingest ----------
def ingest_tree(cfg, store, dita_dir, effective_date, source_version, source_commit,
                source=GITHUB_SOURCE, is_bootstrap=False, label=""):
    """Chunk a full dita tree and merge it as one edition snapshot.  Returns stats."""
    c = dict(cfg)
    c["input_dir"] = dita_dir
    c.pop("files", None)
    c["source_version"] = source_version
    c["pipeline_version"] = _pipeline_rev()

    print(f"[{label or effective_date}] chunking {dita_dir} …", flush=True)
    rows, manifest, _ = chunker.run_chunker(c, progress=True)
    print(f"[{label or effective_date}] {len(rows)} chunks "
          f"({manifest['processed_count']} files, {manifest['skipped_count']} skipped) "
          f"-> merging at effective_date={effective_date}", flush=True)

    stats = store.merge_snapshot(rows, effective_date, source_version,
                                 source=source, source_commit=source_commit)

    lsa_path = os.path.join(dita_dir, cfg.get("lsa_file") or "LSATable.dita")
    entries = changelog.parse_lsa(lsa_path, cfg["regulation"], source_version,
                                  c["pipeline_version"])
    n_lsa = accumulate_changelog(store, entries, source_version, effective_date)
    validation = None if is_bootstrap else lsa_compare(stats["sections_changed"], entries)

    store.save()

    report = {"label": label, "effective_date": effective_date,
              "source_version": source_version, "source_commit": source_commit,
              "source": source, "ingested_at": utcnow(), "bootstrap": is_bootstrap,
              "chunks": len(rows), "lsa_entries": n_lsa,
              "stats": {k: v for k, v in stats.items() if k != "sections_changed"},
              "sections_changed": stats["sections_changed"],
              "lsa_validation": validation}
    rdir = os.path.join(store.dir, "reports")
    os.makedirs(rdir, exist_ok=True)
    rname = f"ingest_{effective_date}_{(source_commit or 'local')[:7]}.json"
    json.dump(report, open(os.path.join(rdir, rname), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    s = stats
    print(f"[{label or effective_date}] merged: unchanged={s['unchanged']} new={s['new']} "
          f"changed={s['changed']} closed={s['closed']} reopened={s['reopened']} "
          f"errata_replaced={s['errata_replaced']} errata_removed={s['errata_removed']} "
          f"backfilled={s['backfilled']} extended_back={s['extended_backward']}", flush=True)
    if validation:
        v = validation
        print(f"[{label or effective_date}] LSA check: {v['agreement']} agree, "
              f"{len(v['lsa_not_detected'])} in LSA not detected, "
              f"{len(v['detected_not_in_lsa'])} detected not in LSA "
              f"-> store/reports/{rname}", flush=True)
    return stats


def _store_for(cfg, args):
    sdir = getattr(args, "store_dir", None) or cfg.get("store_dir") \
        or os.path.join(HERE, "store")
    return Store(sdir, cfg["regulation"])


# ---------- `update`: ingest the clone's current state ----------
def cmd_update(cfg, args):
    store = _store_for(cfg, args)
    input_dir = cfg["input_dir"]
    repo = getattr(args, "repo", None)
    if not repo:
        out = _git(input_dir, "rev-parse", "--show-toplevel", ok_fail=True)
        repo = out.strip() if out else None

    head = _git(repo, "rev-parse", "HEAD").strip() if repo else ""
    state = store.load_state().get(GITHUB_SOURCE, {})
    if repo and head and state.get("last_commit") == head and not args.force:
        print(f"up to date: store already at commit {head[:9]} "
              f"({state.get('last_rev', '?')}) -- use --force to re-ingest")
        return

    mappath = os.path.join(input_dir, cfg.get("ditamap") or "FAR.ditamap")
    rev = chunker.parse_ditamap(mappath)[0]
    if not rev:
        sys.exit(f"cannot read ditamap rev from {mappath}")
    eff = getattr(args, "effective_date", None) or parse_effective_date(rev)
    if not eff:
        sys.exit(f"cannot parse an effective date from ditamap rev '{rev}' "
                 f"(interim edition?) -- pass --effective-date YYYY-MM-DD")

    last_fac, this_fac = fac_id(state.get("last_rev", "")), fac_id(rev)
    mode = ("bootstrap" if not store.editions else
            "new-edition" if this_fac != last_fac else "errata-pass")
    print(f"update mode: {mode}  rev='{rev}'  effective={eff}  commit={head[:9] or 'n/a'}")
    if repo and state.get("last_commit"):
        diff = _git(repo, "diff", "--name-only", f"{state['last_commit']}..{head}",
                    "--", "*.dita", ok_fail=True)
        files = [f for f in (diff or "").splitlines() if f.strip()]
        print(f"  git reports {len(files)} changed .dita file(s) since {state['last_commit'][:9]}")

    ingest_tree(cfg, store, input_dir, eff, rev, head, source=GITHUB_SOURCE,
                is_bootstrap=(mode == "bootstrap"), label=mode)

    store.save_state(GITHUB_SOURCE, {"last_commit": head, "last_rev": rev,
                                     "last_effective_date": eff, "updated_at": utcnow()})
    print(f"store: {store.path}  rows={len(store.rows)}  editions={len(store.editions)}")


# ---------- `replay`: historical editions through the same path ----------
def _edition_plan(repo, branch):
    """Walk first-parent history; return [{fac, settled_sha, rev, first_sha}] in order.
    The settled commit of an edition is the last first-parent commit before the next
    edition's rev first appears (HEAD for the newest edition) -- editions replay from
    their settled state so interim rev text ('August XX') and mid-FAC churn fold in."""
    fp = _git(repo, "log", "--first-parent", "--reverse", "--format=%H", branch).split()
    touched = _git(repo, "log", "--first-parent", "--reverse", "--format=%H", branch,
                   "--", DITAMAP_REL).split()
    touched_set = set(touched)
    eras, cur = [], None
    for i, sha in enumerate(fp):
        if sha in touched_set:
            rev = rev_at_commit(repo, sha)
            f = fac_id(rev) or (rev or "?")
            if f != cur:
                eras.append({"fac": f, "start": i})
                cur = f
    for j, e in enumerate(eras):
        e["settled_idx"] = eras[j + 1]["start"] - 1 if j + 1 < len(eras) else len(fp) - 1
        e["settled_sha"] = fp[e["settled_idx"]]
        e["first_sha"] = fp[e["start"]]
        e["rev"] = rev_at_commit(repo, e["settled_sha"]) or ""
    return eras, fp


def cmd_replay(cfg, args):
    store = _store_for(cfg, args)
    repo = args.repo
    branch = getattr(args, "branch", None)
    if not branch:
        out = _git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD", ok_fail=True)
        branch = out.strip() if out else "HEAD"
    print(f"replay: repo={repo}  branch={branch}")

    eras, _ = _edition_plan(repo, branch)
    since = (getattr(args, "since", None) or "").replace("FAC", "").strip() or None
    plan = [e for e in eras if re.match(r"^\d{4}-\d+$", e["fac"])
            and (since is None or e["fac"] >= since)]
    if getattr(args, "limit_editions", None):
        plan = plan[:args.limit_editions]
    if not plan:
        sys.exit(f"no editions match --since {since} "
                 f"(found: {', '.join(e['fac'] for e in eras)})")

    print("editions to replay: " + ", ".join(
        f"{e['fac']}@{e['settled_sha'][:9]}" for e in plan))

    for n, e in enumerate(plan):
        eff = parse_effective_date(e["rev"])
        if not eff:
            print(f"[{e['fac']}] SKIP: unparseable effective date in rev '{e['rev']}'")
            continue
        passes = [(e["settled_sha"], False)]
        # --errata-check: on the final edition, first ingest an earlier commit of the
        # same FAC whose dita tree differs, so the settled pass hits the errata path.
        if getattr(args, "errata_check", False) and n == len(plan) - 1:
            settled_tree = _git(repo, "rev-parse", f"{e['settled_sha']}:dita").strip()
            first_tree = _git(repo, "rev-parse", f"{e['first_sha']}:dita",
                              ok_fail=True)
            if first_tree and first_tree.strip() != settled_tree \
                    and e["first_sha"] != e["settled_sha"]:
                passes.insert(0, (e["first_sha"], True))
                print(f"[{e['fac']}] errata-check: pre-ingesting {e['first_sha'][:9]} "
                      f"before settled {e['settled_sha'][:9]}")
            else:
                print(f"[{e['fac']}] errata-check: no differing pre-settled commit found; "
                      f"skipping")
        for sha, pre in passes:
            tmp = tempfile.mkdtemp(prefix=f"far_replay_{e['fac']}_")
            try:
                dita_dir = export_dita(repo, sha, tmp)
                label = f"{e['fac']}{'/pre' if pre else ''}"
                # every commit of a FAC merges under the FAC's SETTLED effective date
                ingest_tree(cfg, store, dita_dir, eff, e["rev"], sha,
                            source=GITHUB_SOURCE,
                            is_bootstrap=(not store.editions), label=label)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    last = plan[-1]
    store.save_state(GITHUB_SOURCE, {
        "last_commit": last["settled_sha"], "last_rev": last["rev"],
        "last_effective_date": parse_effective_date(last["rev"]),
        "updated_at": utcnow()})
    print(f"replay complete: store={store.path}  rows={len(store.rows)}  "
          f"editions={len(store.editions)}")
    problems = store.verify()
    print(f"invariant check: {'OK' if not problems else f'{len(problems)} PROBLEM(S)'}")
    for p in problems[:20]:
        print(f"  ! {p}")
