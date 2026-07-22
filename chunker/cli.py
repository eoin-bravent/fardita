#!/usr/bin/env python3
"""chunker — the single entrypoint (replaces the 13 per-module CLIs).

  python -m chunker.cli build     --agency ALL [--base stores_staging]
  python -m chunker.cli certify   --agency DFARS
  python -m chunker.cli dates     --agency ALL
  python -m chunker.cli verify    --agency ALL          # gate the (staging) build vs BASELINE
  python -m chunker.cli dashboard [--port 8643]
"""
import os
import sys
import json
import argparse
import subprocess

from chunker import paths
from chunker import dates as dates_mod
from chunker.state import State
from chunker.store import Store
# `build` (parsers -> lxml) and `audit.certify` (-> lxml) are imported LAZILY inside their verbs, so
# the credential-free / LLM verbs (references, dates, verify, dashboard) run WITHOUT lxml — e.g.
# running the reference pass on a remote box that has only the built JSON stores + Python stdlib.


def _agencies(arg):
    if arg.upper() in ("ALL", "*"):
        return paths.agencies()
    return [a.strip().upper() for a in arg.split(",") if a.strip()]


def _build_subprocess(ag, args):
    cmd = [sys.executable, "-m", "chunker.cli", "build", "--agency", ag,
           "--save-every", str(args.save_every)]
    if args.base:
        cmd += ["--base", args.base]
    if args.no_canon:
        cmd.append("--no-canon")
    if args.no_companions:
        cmd.append("--no-companions")
    if args.fresh:
        cmd.append("--fresh")
    if args.force:
        cmd.append("--force")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run(cmd, cwd=paths.ROOT, env=env).returncode


def cmd_build(args):
    from chunker import build as build_mod        # lazy: pulls in parsers -> lxml (build-only)
    ags = _agencies(args.agency)
    base = args.base or None
    if args.parallel > 1 and len(ags) > 1:
        # process-level parallelism: each agency is an independent store, so N agencies
        # build concurrently as N `chunker build --agency X` child processes (mirrors the
        # old orchestrator --parallel). One bad agency can't stop the others.
        from concurrent.futures import ThreadPoolExecutor
        print(f"building {len(ags)} agencies, {args.parallel} at a time")
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            list(ex.map(lambda a: _build_subprocess(a, args), ags))
        return
    for ag in ags:
        try:
            build_mod.build_agency(ag, base=base, do_canon=not args.no_canon,
                                   companions=not args.no_companions,
                                   save_every=args.save_every, fresh=args.fresh, force=args.force)
        except Exception as e:
            print(f"[{ag}] BUILD FAILED: {e!r}")


def cmd_certify(args):
    from chunker.audit import certify as cert_mod   # lazy: pulls in corpus_audit -> lxml (certify-only)
    base = args.base or None
    write = getattr(args, "write", False)
    for ag in _agencies(args.agency):
        sd = paths.store_dir(ag, base)
        c = cert_mod.certify(ag, store_dir=sd)
        v = c["verification"]
        comp = c["companion"]
        if write:
            # SAFE re-audit: rewrite state.json from the audit only -- never re-ingests (mirrors
            # build.py's state write). Use this after an audit-logic change; do NOT re-run
            # `build` non-fresh on a finished store (that re-ingests and can corrupt it).
            stt = State(sd, ag)
            stt.set_section("verification", v)
            if comp:
                stt.set_section("companion", comp)
            if c.get("dates"):
                stt.set_section("dates", c["dates"])
            with open(os.path.join(sd, "review_queue.json"), "w", encoding="utf-8") as f:
                json.dump(c["review_queue"], f, indent=1, ensure_ascii=False)
            stt.mark_step("audit",
                          "ok" if v["invariants_ok"] and not v["missing_sections"] else "ATTENTION",
                          covered=v["covered_pct"], accounted=v["accounted_pct"],
                          missing=v["missing_sections"])
        line = (f"{ag:10} covered(min)={v['covered_pct']} accounted={v['accounted_pct']} "
                f"missing={v['missing_sections']} inv={v['invariants_ok']}")
        if comp:
            line += f" | companion {comp['current_units']}u {comp['captured_pct']}% body-bearing"
            if comp.get("out_of_ditamap_total"):
                line += f" (+{comp['out_of_ditamap_total']} out-of-ditamap)"
        if write:
            line += "  [state written]"
        print(line)


def cmd_dates(args):
    base = args.base or None
    for ag in _agencies(args.agency):
        st = Store(paths.store_dir(ag, base), ag)
        c = dates_mod.date_coverage(st)
        print(f"{ag:10} eff {c['editions_with_effective']}/{c['editions']}  "
              f"commit {c['rows_git_with_commit']}/{c['rows_git']} git rows  "
              f"{'OK' if c['ok'] else 'GAP'}")


def cmd_verify(args):
    """Gate a (staging) build against docs/BASELINE.json: invariants clean, 0 missing, dates
    100%, covered% not regressed, AND canon actually ran (unless the agency has no GitHub
    source). Edition COUNT is NOT gated (marker replay grows it; honest reg covered%
    legitimately RISES as companions leave the denominator)."""
    from chunker.ingest.canon import NO_REPO       # agencies with no upstream GitHub repo
    base = json.load(open(os.path.join(paths.ROOT, "docs", "BASELINE.json"),
                          encoding="utf-8"))["agencies"]
    allok = True
    for ag in _agencies(args.agency):
        stt = State(paths.store_dir(ag, args.base or None), ag).get()
        v = stt.get("verification", {})
        dc = stt.get("dates", {})
        b = base.get(ag, {})
        inv, miss, cov = v.get("invariants_ok"), v.get("missing_sections"), v.get("covered_pct")
        cov_ok = (cov is None or b.get("covered") is None or cov >= b["covered"] - 0.5)
        # canon must have run, else the agency is silently archive-only (missing its GitHub
        # current edition + companions) yet still passes coverage/invariants/dates -- the AGAR
        # dead-symlink gap. NO_REPO agencies (e.g. TRANSFARS) legitimately have no canon step.
        canon_status = (stt.get("steps", {}).get("canon") or {}).get("status")
        canon_ok = (ag in NO_REPO) or (canon_status == "ok")
        ok = bool(inv) and miss == 0 and bool(dc.get("ok")) and cov_ok and canon_ok
        allok &= ok
        cflag = "n/a" if ag in NO_REPO else (canon_status or "MISSING")
        print(f"{ag:10} inv={str(inv):5} missing={miss} dates_ok={dc.get('ok')} "
              f"canon={cflag:7} covered={cov} (baseline {b.get('covered')})  [{'PASS' if ok else 'FAIL'}]")
    print("VERDICT:", "PASS" if allok else "FAIL")
    sys.exit(0 if allok else 1)


def cmd_dashboard(args):
    from chunker import dashboard          # lazy: only needed for this verb
    dashboard.main(args.port, args.base or None)


def cmd_references(args):
    """Post-ingest, OFFLINE reference-verification pass over already-built stores: blind LLM
    audit of each unit's text -> reconcile vs parser refs -> (judge) -> apply verified refs +
    stamp refs_verified_from. Never part of `build`. Loops agencies; each store is independent."""
    from chunker.references import run as references_run   # lazy: pulls in the LLM transport
    base = args.base or None
    agencies = _agencies(args.agency)
    if args.normalize_only:                                # credential-free deterministic pass (NO LLM)
        index = references_run.build_temporal_index(base)  # ONE global temporal index across all agencies
        for ag in agencies:
            try:
                references_run.normalize_store(ag, base=base, index=index)
            except Exception as e:
                print(f"[{ag}] NORMALIZE FAILED: {e!r}")
        return
    # ONE global temporal index across all agencies -> lets the pass resolve cross-regulation refs
    # (any agency -> any agency) as-of each unit's edition. Built once, shared across agencies.
    index = references_run.build_temporal_index(base)
    for ag in agencies:
        try:
            if args.all_history:                           # audit EVERY distinct version, not just current
                references_run.run_references_history(
                    ag, base=base, index=index, files=args.files, judge=args.judge,
                    auto_accept=args.auto_accept, mock_llm=args.mock_llm, limit=args.limit,
                    provider=args.provider, concurrency=args.concurrency)
                continue
            s = references_run.run_references(
                ag, base=base, index=index, as_of=args.as_of, files=args.files, judge=args.judge,
                auto_accept=args.auto_accept, mock_llm=args.mock_llm, limit=args.limit,
                provider=args.provider, concurrency=args.concurrency)
            if s.get("status") in ("queue-empty", "empty-store", "no-rows-in-force"):
                print(f"{ag:10} {s['status']}"
                      + ("  (all current rows already carry verified refs)"
                         if s.get("verified_already") else ""))
        except Exception as e:
            print(f"[{ag}] REFERENCES FAILED: {e!r}")


def cmd_update(args):
    """Idempotent GitHub update of already-built stores: clone-or-pull every repo (parallel,
    deduped by physical repo so DFARS/DFARSPGI never race) -> canon-only incremental ingest
    (skips the static archive backfill) -> optional references on the unstamped delta -> verify.
    Every step is idempotent, so an update with no upstream change is a no-op."""
    from chunker.ingest import canon as fc          # lazy: pulls lxml (via parsers) only on ingest
    from chunker.ingest import archive_download as ad
    from chunker import update as upd
    from chunker import build as build_mod
    base = args.base or None
    ags = _agencies(args.agency)
    conc = args.concurrency or 8
    git_ags = [a for a in ags if upd.update_via(a) != "archive"]     # GitHub clone/pull + replay
    arch_ags = [a for a in ags if upd.update_via(a) == "archive"]    # acquisition.gov scrape + backfill

    if not args.no_fetch:                            # 1. fetch: git repos in parallel; archives scraped
        if git_ags:
            res = fc.download_all(git_ags, concurrency=conc)
            if res["errors"]:
                print(f"[update] {len(res['errors'])} repo(s) failed to fetch: "
                      f"{', '.join(sorted(res['errors']))} -- their agencies report no-clone below")
        for ag in arch_ags:
            try:
                ad.download_archives(ag)
            except Exception as e:
                print(f"[{ag}] ARCHIVE FETCH FAILED: {e!r}")

    for ag in ags:                                   # 2. ingest: git -> canon-only; archive -> backfill(+canon)
        try:
            if ag in arch_ags:
                build_mod.build_agency(ag, base=base, do_canon=True)
            else:
                s = upd.update_agency(ag, base=base)
                if s.get("status"):
                    print(f"{ag:10} {s['status']}")
        except Exception as e:
            print(f"[{ag}] UPDATE FAILED: {e!r}")

    if args.references:                              # 3. LLM reference pass on the delta (needs creds)
        from chunker.references import run as references_run
        index = references_run.build_temporal_index(base)
        for ag in ags:
            try:
                references_run.run_references_history(
                    ag, base=base, index=index, judge=args.judge,
                    provider=args.provider, concurrency=conc)
            except Exception as e:
                print(f"[{ag}] REFERENCES FAILED: {e!r}")

    if not args.no_verify:                           # 4. gate vs BASELINE (exits PASS/FAIL)
        cmd_verify(args)


def cmd_download_archives(args):
    """Scrape + download acquisition.gov archive editions into archive/<AG> (the acquisition.gov
    content source that feeds the archive backfill). Dependency-free (lxml + stdlib). Idempotent:
    ZIPs/folders already present are skipped, so a re-run fetches only NEW editions."""
    from chunker.ingest import archive_download as ad     # lazy: lxml + network
    for ag in _agencies(args.agency):
        try:
            s = ad.download_archives(ag, overwrite=args.overwrite, delete_zips=args.delete_zips)
            print(f"{ag:10} {s['extracted']} edition(s) ({s['new']} new), "
                  f"{len(s['failed'])} failed -> {s['dir']}")
        except Exception as e:
            print(f"[{ag}] DOWNLOAD-ARCHIVES FAILED: {e!r}")


def main():
    ap = argparse.ArgumentParser(prog="chunker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="survey+backfill+canon(+companions)+certify one/all agencies")
    b.add_argument("--agency", default="ALL")
    b.add_argument("--base", default="", help="target root (e.g. stores_staging); default stores/")
    b.add_argument("--no-canon", action="store_true")
    b.add_argument("--no-companions", action="store_true")
    b.add_argument("--save-every", type=int, default=10)
    b.add_argument("--parallel", type=int, default=1,
                   help="build N agencies concurrently (process-level; each store is independent)")
    b.add_argument("--fresh", action="store_true",
                   help="clean in-place rebuild: archive the prior store to prerebuild/ first")
    b.add_argument("--force", action="store_true",
                   help="with --fresh, rebuild even if store.json already exists (else skip)")
    b.set_defaults(fn=cmd_build)

    for name, fn, hlp in (("certify", cmd_certify, "honest reg coverage + companion summary"),
                          ("dates", cmd_dates, "date-coverage report"),
                          ("verify", cmd_verify, "gate a build vs docs/BASELINE.json")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--agency", default="ALL")
        p.add_argument("--base", default="")
        if name == "certify":
            p.add_argument("--write", action="store_true",
                           help="re-run the audit and rewrite state.json (NO ingest; safe re-audit)")
        p.set_defaults(fn=fn)

    d = sub.add_parser("dashboard", help="serve the fleet dashboard (reads state.json)")
    d.add_argument("--port", type=int, default=8643)
    d.add_argument("--base", default="", help="store root to read (e.g. stores_staging)")
    d.set_defaults(fn=cmd_dashboard)

    rf = sub.add_parser("references",
                        help="OFFLINE ref-verification pass: LLM audit -> reconcile -> apply verified refs (post-ingest)")
    rf.add_argument("--agency", default="ALL")
    rf.add_argument("--base", default="", help="store root (e.g. stores_staging); default stores/")
    rf.add_argument("--as-of", dest="as_of", metavar="YYYY-MM-DD",
                    help="audit the rows in force on ONE date (a single historical slice)")
    rf.add_argument("--all-history", dest="all_history", action="store_true",
                    help="audit EVERY distinct version of every unit across all editions (not just "
                         "current); loops edition dates, each distinct text audited exactly once")
    rf.add_argument("--files", nargs="+", metavar="UNIT",
                    help="audit only these units (e.g. 22.1503) instead of the store's queue")
    rf.add_argument("--provider", choices=["usai", "vertex"], help="LLM backend (default usai)")
    rf.add_argument("--concurrency", type=int, help="parallel LLM calls (default 8)")
    rf.add_argument("--judge", dest="judge", action="store_true", default=None,
                    help="also run the LLM judge over disagreements")
    rf.add_argument("--no-judge", dest="judge", action="store_false")
    rf.add_argument("--mock-llm", dest="mock_llm", metavar="FILE",
                    help="read {citation: [refs]} from FILE instead of calling the API (offline test)")
    rf.add_argument("--limit", type=int, help="audit at most N units (smoke test)")
    rf.add_argument("--no-auto-accept", dest="auto_accept", action="store_false", default=True,
                    help="write the ledger but do NOT apply to the store (inspect first)")
    rf.add_argument("--normalize-only", dest="normalize_only", action="store_true",
                    help="credential-free: clean raw href targets + set target_agency/target_kind on the "
                         "store, no LLM (the deterministic half of the pass; run after a re-ingest)")
    rf.set_defaults(fn=cmd_references)

    up = sub.add_parser("update",
                        help="idempotent GitHub update: clone/pull -> incremental ingest -> "
                             "references(delta) -> verify")
    up.add_argument("--agency", default="ALL")
    up.add_argument("--base", default="", help="store root (default stores/)")
    up.add_argument("--concurrency", type=int, help="parallel git fetches + LLM calls (default 8)")
    up.add_argument("--no-fetch", dest="no_fetch", action="store_true",
                    help="skip clone/pull; ingest from the existing clones")
    up.add_argument("--references", dest="references", action="store_true", default=False,
                    help="also run the LLM reference pass on the delta (needs credentials)")
    up.add_argument("--judge", dest="judge", action="store_true", default=None)
    up.add_argument("--no-judge", dest="judge", action="store_false")
    up.add_argument("--provider", choices=["usai", "vertex"])
    up.add_argument("--no-verify", dest="no_verify", action="store_true",
                    help="skip the closing BASELINE verify gate")
    up.set_defaults(fn=cmd_update)

    da = sub.add_parser("download-archives",
                        help="scrape+download acquisition.gov archive editions into archive/<AG> "
                             "(the website content source; feeds backfill). dependency-free")
    da.add_argument("--agency", default="ALL")
    da.add_argument("--overwrite", action="store_true",
                    help="re-download + re-extract editions that already exist")
    da.add_argument("--delete-zips", dest="delete_zips", action="store_true",
                    help="delete each ZIP after successful extraction")
    da.set_defaults(fn=cmd_download_archives)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
