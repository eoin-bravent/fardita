#!/usr/bin/env python3
"""Incremental GitHub update of ALREADY-BUILT stores: canon-only re-ingest, no archive backfill.

This is the steady-state counterpart to `build`. `build` does survey + archive backfill + canon
+ certify (a from-scratch history); an UPDATE only needs the new GitHub editions, so it skips the
static archive re-parse entirely and just runs canon (replay + ingest_head) over the refreshed
clone.

Every step is idempotent, so re-running with no upstream change is a no-op:
  * canon.replay / ingest_head skip commits whose SHA the store already recorded (_done_shas);
  * store.merge_snapshot compares each unit's content_hash to the row in force -> unchanged text
    is a no-op that KEEPS the row's verified refs + refs_verified_from stamp; only genuinely
    changed text opens a fresh (unstamped) row, which the references pass then re-audits.

Assumes the clone exists (run chunker.ingest.canon.download / download_all first). Orchestrated by
cli.cmd_update, which fetches clones in parallel, then runs the reference pass on the delta.
"""
import os

from chunker import paths
from chunker.state import State
from chunker.store import Store
from chunker.ingest import canon as fc


def update_via(ag):
    """Which source an UPDATE pulls this agency from: 'archive' (acquisition.gov scrape + backfill)
    or 'git' (clone/pull + replay). Config-driven via agencies.json `update_via`; default 'git'.
    DFARSPGI is 'archive' -- GitHub does not carry its PGI content (see canon.ALIAS / repos_for)."""
    return (paths.agencies_cfg().get(ag) or {}).get("update_via", "git")


def update_agency(ag, *, base=None, companions=True, save_every=10, log=print):
    """Pull-then-ingest ONE agency from its GitHub clone only (archive backfill skipped). Refreshes
    certification + date coverage best-effort (tolerated if the archive tree is absent on the box).
    Returns a summary dict; a `status` key means it was skipped (no store / no repo / no clone)."""
    sdir = paths.store_dir(ag, base)
    if not os.path.exists(os.path.join(sdir, "store.json")):
        return {"agency": ag, "status": "no-store (build first)"}
    if ag in fc.NO_REPO:
        return {"agency": ag, "status": "no-repo (archive-only)"}
    repo = fc.repo_dir(ag)
    if not os.path.isdir(os.path.join(repo, ".git")):
        return {"agency": ag, "status": "no-clone (run download / update without --no-fetch)"}

    state = State(sdir, ag)
    store = Store(sdir, ag)
    before = len(store.rows)
    cstore = Store(sdir, f"{ag}-COMPANION", name="companion") if companions else None
    crep = fc.replay(store, ag, companion_store=cstore, save_every=save_every)
    hres = fc.ingest_head(store, ag, companion_store=cstore) or {}

    new_eds = len(crep.get("editions", []))
    head = hres.get("sha") or hres.get("note", "")
    added = len(store.rows) - before

    cov = None                                              # audit/date refresh is best-effort:
    try:                                                    # the archive tree may be absent on a
        from chunker.audit import certify as cert_mod       # references-only remote box.
        from chunker import dates as dates_mod
        c = cert_mod.certify(ag, store_dir=sdir, archives_dir=paths.archive_dir(ag))
        state.set_section("verification", c["verification"])
        if c.get("companion"):
            state.set_section("companion", c["companion"])
        state.set_section("dates", dates_mod.date_coverage(store))
        cov = c["verification"].get("covered_pct")
    except Exception as e:
        cov = f"(certify skipped: {type(e).__name__})"

    state.mark_step("canon", "ok", editions=new_eds, head=(hres.get("sha") or None),
                    skipped=len(crep.get("skipped", [])))
    state.mark_step("update", "ok", gh_editions=new_eds, rows_added=added)
    log(f"[{ag}] update: +{new_eds} gh edition(s), HEAD {str(head)[:12]}, "
        f"rows {before}->{len(store.rows)} (+{added}); covered={cov}")
    return {"agency": ag, "gh_editions": new_eds, "rows_added": added, "head": head}
