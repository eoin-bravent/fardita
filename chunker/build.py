"""Per-agency build/rebuild orchestration — the clean replacement for orchestrator.py.

One agency, from scratch, into a target store dir (staging or live):
  survey  -> classify archive era folders (chunker/cache/<ag>_eras.json)
  backfill-> ingest archive editions oldest-first (chunker.ingest.archive)
  canon   -> replay GitHub editions via the agency's edition source, capturing companions
             into companion.json (chunker.ingest.canon + editions)
  certify -> honest reg-only coverage + companion summary (chunker.audit.certify)
  dates   -> date coverage
All results are folded into ONE stores/<AG>/state.json (chunker.state). Every underlying
step is the already-verified ported logic; this module only sequences them + records state.
"""
import os
import json
from collections import Counter

from chunker import paths
from chunker.state import State
from chunker.store import Store
from chunker.parsers import _adapter as A
from chunker.ingest import archive as fa
from chunker.ingest import canon as fc
from chunker.audit import certify as cert_mod
from chunker import dates as dates_mod


def _archive_prior(sdir):
    """Move prior store artifacts aside to <sdir>/prerebuild/ (non-destructive) so a fresh
    rebuild starts clean — the automatic in-place backup, like the old rebuild step. Keeps
    *.archive-built-bak (the FAR reference oracle). Returns the backup dir or None."""
    import shutil

    def take(f):
        return (f.endswith("_store.json") or f.endswith("_errata.json")
                or f in ("store.json", "companion.json", "errata.json", "state.json",
                         "store.prev.json", "companion.prev.json", "pipeline_state.json",
                         "verification.json", "date_coverage.json", "review_queue.json",
                         "backfill_report.json", "collapse_audit.json")
                or ".bak-" in f or ".pre-rebuild-" in f)

    prior = [f for f in os.listdir(sdir)
             if os.path.isfile(os.path.join(sdir, f)) and take(f)
             and not f.endswith(".archive-built-bak")]
    if not prior:
        return None
    dest = os.path.join(sdir, "prerebuild")
    first_time = not os.path.isdir(dest)       # preserve the ORIGINAL pre-chunker backup
    os.makedirs(dest, exist_ok=True)
    for f in prior:
        src = os.path.join(sdir, f)
        try:
            if first_time:
                shutil.move(src, os.path.join(dest, f))
            else:
                os.remove(src)                 # original already saved; drop the rebuilt copy
        except OSError:
            pass
    return dest


def survey(agency, archives_dir):
    """Classify each archive edition folder -> chunker/cache/<ag>_eras.json {era, effective_date}."""
    profile = A.ensure_profile(agency)
    dpath = profile.get("dates", "")
    if not dpath or not os.path.exists(dpath):
        sib = os.path.join(archives_dir, "archive_metadata.json")
        dpath = sib if os.path.exists(sib) else dpath
    date_map = A.load_archive_dates(dpath) if dpath and os.path.exists(dpath) else {}
    out = {}
    for folder in sorted(os.listdir(archives_dir)):
        p = os.path.join(archives_dir, folder)
        if not os.path.isdir(p):
            continue
        out[folder] = {"era": A.classify_folder(p)[0],
                       "effective_date": (date_map.get(folder) or {}).get("effective_date", "")}
    ep = A.eras_path(agency)
    os.makedirs(os.path.dirname(ep), exist_ok=True)
    with open(ep, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    return out


def build_agency(agency, *, base=None, do_canon=True, companions=True,
                 save_every=10, fresh=False, force=False, log=print):
    """Build one agency into paths.store_dir(agency, base). fresh=True archives any prior
    store aside (to prerebuild/) first — a clean in-place rebuild with an automatic backup,
    no staging/swap. A fresh build SKIPS an agency already rebuilt (store.json present) unless
    force=True, so a fleet rebuild is resumable. Returns the final state dict."""
    sdir = paths.store_dir(agency, base)
    os.makedirs(sdir, exist_ok=True)
    if fresh:
        if os.path.exists(os.path.join(sdir, "store.json")) and not force:
            log(f"[{agency}] already rebuilt (store.json present) -- skipping (force to redo)")
            return State(sdir, agency).get()
        arch = _archive_prior(sdir)
        if arch:
            log(f"[{agency}] archived prior store -> {os.path.relpath(arch, paths.ROOT)}")
    state = State(sdir, agency)
    archives_dir = paths.archive_dir(agency)
    log(f"[{agency}] build -> {sdir}")

    # 1. survey
    if os.path.isdir(archives_dir):
        eras = survey(agency, archives_dir)
        state.mark_step("survey", "ok", eras=dict(Counter(v["era"] for v in eras.values())))
    else:
        state.mark_step("survey", "skip", note="no archive on disk")

    # 2. archive backfill (the pre-repo history)
    store = Store(sdir, agency)
    if os.path.isdir(archives_dir):
        rep = fa.backfill(store, agency, archives_dir, save_every=save_every,
                          verify_every=max(save_every, 10))
        state.mark_step("backfill", "ok" if "invariant_failure" not in rep else "FAIL",
                        rows=len(store.rows), editions=len(store.editions),
                        ingested=len([e for e in rep["editions"] if e.get("chunks")]))
        log(f"[{agency}] archive: rows={len(store.rows)} editions={len(store.editions)}")

    # 3. canon replay (+ companion capture) via the agency's edition source
    cstore = (Store(sdir, f"{agency}-COMPANION", name="companion")
              if companions and agency not in fc.NO_REPO else None)
    if do_canon and agency not in fc.NO_REPO:
        repo = fc.repo_dir(agency)
        if os.path.isdir(os.path.join(repo, ".git")):
            try:
                crep = fc.replay(store, agency, companion_store=cstore, save_every=save_every)
                # always ensure the CURRENT HEAD is ingested (dated via canon_dates.json), so
                # agencies whose historical markers are all unknown-date still capture the
                # current published edition + its companions (skips if HEAD was a replayed edition)
                hres = fc.ingest_head(store, agency, companion_store=cstore)
                state.mark_step("canon", "ok", editions=len(crep["editions"]),
                                skipped=len(crep["skipped"]), head=(hres or {}).get("sha"),
                                companion_editions=len(crep.get("companion_editions", [])))
                log(f"[{agency}] canon: +{len(crep['editions'])} gh editions "
                    f"(skipped {len(crep['skipped'])}) + HEAD {(hres or {}).get('sha', '-')}, "
                    f"companion editions {len(crep.get('companion_editions', []))}")
            except Exception as e:
                state.mark_step("canon", "FAIL", note=repr(e)[:200])
                log(f"[{agency}] canon FAILED: {e!r}")
        else:
            state.mark_step("canon", "skip", note="no clone (run download)")

    # 4. certify (honest reg + companion) + dates -> state
    c = cert_mod.certify(agency, store_dir=sdir, archives_dir=archives_dir)
    state.set_section("verification", c["verification"])
    if c["companion"]:
        state.set_section("companion", c["companion"])
    state.set_section("dates", dates_mod.date_coverage(store))
    with open(os.path.join(sdir, "review_queue.json"), "w", encoding="utf-8") as f:
        json.dump(c["review_queue"], f, indent=1, ensure_ascii=False)
    v = c["verification"]
    state.mark_step("audit", "ok" if v["invariants_ok"] and not v["missing_sections"] else "ATTENTION",
                    covered=v["covered_pct"], accounted=v["accounted_pct"],
                    missing=v["missing_sections"])
    log(f"[{agency}] audit: covered(min)={v['covered_pct']} missing={v['missing_sections']} "
        f"invariants={'clean' if v['invariants_ok'] else 'FAIL'}"
        + (f" | companion units={c['companion']['current_units']} "
           f"({c['companion']['captured_pct']}% body-bearing captured)"
           if c["companion"] else ""))
    return state.get()
