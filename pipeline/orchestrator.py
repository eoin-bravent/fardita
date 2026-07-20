#!/usr/bin/env python3
"""All-in-one pipeline orchestrator: acquisition.gov archives -> versioned stores.

One engine, per agency, five steps (each resumable, each recorded in the agency's
pipeline_state.json so the dashboard and the CLI always agree):

  download   pull every archive zip + metadata from acquisition.gov
             (download_acquisition_archives.py)
  survey     classify the edition folders into parser eras (archive_adapter survey)
  backfill   chunk + merge every edition into stores/<AGENCY>/ oldest-first, canon
             eras (dita/github) before HTML backfill eras (backfill_archives.py)
  audit      verification: store invariants + text-conservation on sampled editions;
             ambiguous residue -> review_queue.json for optional LLM triage
  llm        (optional) reference audit through the existing LLM path
             (pipeline.py audit [--judge] [--auto-accept]) -- run any time later

Usage:
  python orchestrator.py status [--agency AFARS]
  python orchestrator.py run --agency AFARS [--steps download,survey,backfill,audit]
  python orchestrator.py run --agency AFARS --steps llm [--judge] [--auto-accept]

Layout (relative to the repo root, i.e. the parent of pipeline/):
  archive/<AGENCY>/...          downloaded editions + archive_metadata.json
  stores/<AGENCY>/              the agency's store + state + reports
"""
import os
import re
import sys
import json
import time
import glob
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import archive_adapter as A
from store import Store

PY = sys.executable
DEFAULT_STEPS = ["download", "survey", "backfill", "audit"]
CANON_ERAS = ["dita"]                       # source-of-truth eras ingest FIRST
HTML_ERAS = ["ditaot", "agov", "transit", "webworks-2005", "webworks-2001",
             "legacy", "pandoc"]


def agencies():
    return json.load(open(os.path.join(HERE, "data", "agencies.json"), encoding="utf-8"))


def archive_dir(agency):
    """Where this agency's downloaded editions live (existing layouts respected)."""
    for c in (os.path.join(ROOT, "archive", agency),
              os.path.join(ROOT, f"archive_{agency.lower()}", agency),
              os.path.join(ROOT, f"archive_{agency.lower()}")):
        if os.path.isdir(c):
            return c
    return os.path.join(ROOT, "archive", agency)


def store_dir(agency):
    d = os.path.join(ROOT, "stores", agency)
    # FAR predates the app: its live store is pipeline/store. Use it unless a
    # stores/FAR store has been created explicitly.
    if agency == "FAR" and not os.path.exists(os.path.join(d, "FAR_store.json")) \
            and os.path.exists(os.path.join(HERE, "store", "FAR_store.json")):
        return os.path.join(HERE, "store")
    os.makedirs(d, exist_ok=True)
    return d


def state_path(agency):
    return os.path.join(store_dir(agency), "pipeline_state.json")


def load_state(agency):
    p = state_path(agency)
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {"agency": agency, "steps": {}, "log": []}


def save_state(agency, state):
    tmp = state_path(agency) + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, state_path(agency))


def mark(state, step, status, **info):
    state["steps"][step] = {"status": status, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            **info}
    state["log"].append(f"{time.strftime('%H:%M:%S')} {step}: {status} "
                        + json.dumps(info, default=str)[:200])
    state["log"] = state["log"][-200:]


def run_cmd(args_list, cwd=HERE, log_to=None, progress_cb=None):
    """Run a subprocess, streaming output to console/log; progress_cb(line) per line."""
    print("  $", " ".join(str(a) for a in args_list), flush=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    p = subprocess.Popen([str(a) for a in args_list], cwd=cwd, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
    lines, buf = [], ""
    # split on \r as well as \n: tqdm progress bars redraw with carriage returns and
    # would otherwise emit NOTHING until a whole download finishes (looks hung)
    for chunk in iter(lambda: p.stdout.read(256), ""):
        buf += chunk
        while True:
            m = re.search(r"[\r\n]", buf)
            if not m:
                break
            line, buf = buf[:m.start()], buf[m.end():]
            if not line.strip():
                continue
            print("   ", line, flush=True)
            lines.append(line + "\n")
            if log_to:
                log_to.write(line + "\n")
                log_to.flush()
            if progress_cb:
                try:
                    progress_cb(line)
                except Exception:
                    pass
    p.wait()
    return p.returncode, "".join(lines[-100:])


# ---------------------------------------------------------------- steps
def step_download(agency, state):
    out_root = os.path.dirname(archive_dir(agency)) or os.path.join(ROOT, "archive")
    os.makedirs(out_root, exist_ok=True)
    last_save = [0.0]

    def cb(line):
        if not line.strip():
            return
        st = state["steps"].setdefault("download", {"status": "running"})
        st["progress"] = line.strip()[-140:]
        st["zips_seen"] = st.get("zips_seen", 0) + (1 if ".zip" in line.lower() else 0)
        if time.time() - last_save[0] > 2:          # live progress for the dashboard
            save_state(agency, state)
            last_save[0] = time.time()

    rc, tail = run_cmd([PY, os.path.join(HERE, "download_acquisition_archives.py"),
                        "--agency", agency, "--output", out_root], progress_cb=cb)
    n = len([d for d in glob.glob(os.path.join(archive_dir(agency), "*")) if os.path.isdir(d)])
    mark(state, "download", "ok" if rc == 0 else "FAIL", editions=n, rc=rc)
    return rc == 0


def step_survey(agency, state):
    adir = archive_dir(agency)
    eras_out = os.path.join(HERE, "cache", f"{agency.lower()}_eras.json" if agency != "FAR"
                            else "archive_eras.json")
    rc, tail = run_cmd([PY, os.path.join(HERE, "archive_adapter.py"), "survey", adir,
                        "--regulation", agency, "--out", eras_out])
    eras = {}
    if os.path.exists(eras_out):
        m = json.load(open(eras_out, encoding="utf-8"))
        for v in m.values():
            eras[v["era"]] = eras.get(v["era"], 0) + 1
    unparseable = {e: n for e, n in eras.items() if e not in A.ERA_CHUNKERS
                   and e not in ("empty", "fm-source")}
    mark(state, "survey", "ok" if rc == 0 else "FAIL", eras=eras,
         needs_new_parser=unparseable, rc=rc)
    return rc == 0


def step_backfill(agency, state):
    adir, sdir = archive_dir(agency), store_dir(agency)
    eras_file = os.path.join(HERE, "cache", f"{agency.lower()}_eras.json" if agency != "FAR"
                             else "archive_eras.json")
    present = set()
    if os.path.exists(eras_file):
        present = {v["era"] for v in json.load(open(eras_file, encoding="utf-8")).values()}
    phases = []
    canon = [e for e in CANON_ERAS if e in present]
    html = [e for e in HTML_ERAS if e in present]
    if canon:
        phases.append(canon)                # bootstrap from source-of-truth first
    if html:
        phases.append(html)
    ok = True
    for eras in phases:
        rc, tail = run_cmd([PY, os.path.join(HERE, "backfill_archives.py"),
                            "--store-dir", sdir, "--archives-dir", adir,
                            "--regulation", agency, "--eras", ",".join(eras),
                            "--save-every", "10", "--verify-every", "10",
                            "--audit-out", os.path.join(sdir, "collapse_audit.json"),
                            "--report-out", os.path.join(sdir, "backfill_report.json")])
        ok = ok and rc == 0
    st = Store(sdir, agency)
    stats = {"rows": len(st.rows), "editions": len(st.editions),
             "floor": min((e["effective_date"] for e in st.editions), default=""),
             "ceiling": max((e["effective_date"] for e in st.editions), default="")}
    mark(state, "backfill", "ok" if ok and st.rows else "FAIL", **stats)
    return ok


def step_audit(agency, state, sample=3):
    """Verification: store invariants + conservation audit on sampled editions
    (newest edition of each era). Ambiguous residue -> review_queue.json."""
    adir, sdir = archive_dir(agency), store_dir(agency)
    st = Store(sdir, agency)
    problems = st.verify()
    eras_file = os.path.join(HERE, "cache", f"{agency.lower()}_eras.json" if agency != "FAR"
                             else "archive_eras.json")
    picks = []
    if os.path.exists(eras_file):
        m = json.load(open(eras_file, encoding="utf-8"))
        by_era = {}
        for folder, v in m.items():
            if v["era"] in A.ERA_CHUNKERS and v.get("effective_date"):
                by_era.setdefault(v["era"], []).append((v["effective_date"], folder))
        for era, lst in by_era.items():
            picks.append((era, sorted(lst)[-1][1]))          # newest of each era
    import corpus_audit as CU
    # folders shipping multiple renders: audit only the render the parser reads
    ERA_FILES_RX = {"agov": r"(?i)AFARS[-_]PART[-_]\d+\.html$",
                    "ditaot": r"(FAR|AFARS)[-_](Part|PART)[-_]\d+\.html$",
                    "dita": r"(FAR|AFARS)[-_](Part|PART)[-_]\d+\.html$"}
    queue, certs = [], []
    for era, folder in picks[:sample + len(CANON_ERAS)]:
        edir = os.path.join(adir, folder)
        cfg = A.default_cfg(agency)
        cfg["source_version"] = folder
        cfg["pipeline_version"] = "audit"
        try:
            rows, _ = A.ERA_CHUNKERS[era](edir, cfg, {})
            rep = CU.audit(edir, rows, files_rx=ERA_FILES_RX.get(era))
        except Exception as e:
            certs.append({"era": era, "folder": folder, "error": repr(e)[:200]})
            continue
        t = rep["totals"]
        u = t["residue"].get("UNCLASSIFIED", 0)
        pct = 100 * u / max(t["source_chars"], 1)
        certs.append({"era": era, "folder": folder,
                      "source_chars": t["source_chars"],
                      "covered_pct": round(100 * t["covered_chars"] / max(t["source_chars"], 1), 2),
                      # THE headline: how much of the source is explained --
                      # captured in chunks OR classified skip-by-design boilerplate
                      "accounted_pct": round(100 - pct, 3),
                      "residue": t["residue"],
                      "unclassified_chars": u, "unclassified_pct": round(pct, 3),
                      "samples": [s["text"][:200] for s in rep["unclassified_samples"][:5]],
                      "pass": pct <= 0.5})
        for s in rep["unclassified_samples"]:
            queue.append({"era": era, "folder": folder, **s, "status": "pending"})
    ver = {"invariants_ok": not problems, "invariant_problems": problems[:20],
           "editions": len(st.editions), "rows": len(st.rows),
           "certificates": certs, "review_queue_len": len(queue),
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    json.dump(ver, open(os.path.join(sdir, "verification.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    json.dump(queue, open(os.path.join(sdir, "review_queue.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    ok = not problems and all(c.get("pass") for c in certs if "pass" in c)
    mark(state, "audit", "ok" if ok else "ATTENTION", invariants=not problems,
         certificates=len(certs), review_queue=len(queue))
    return True                                  # audit findings never block the pipeline


def step_llm(agency, state, judge=False, auto_accept=False, triage=False):
    """Optional LLM stages. triage: classify review-queue residue (needs API key).
    Otherwise: the reference audit via the existing pipeline.py path."""
    sdir = store_dir(agency)
    if triage:
        qp = os.path.join(sdir, "review_queue.json")
        queue = json.load(open(qp, encoding="utf-8")) if os.path.exists(qp) else []
        pending = [q for q in queue if q.get("status") == "pending"]
        if not pending:
            mark(state, "llm-triage", "ok", note="queue empty")
            return True
        try:
            import gemini_audit                     # the existing LLM transport
        except Exception as e:
            mark(state, "llm-triage", "SKIP", note=f"LLM transport unavailable: {e}")
            return True
        # one compact call per item batch; reuse the audit transport's raw call if exposed
        mark(state, "llm-triage", "TODO", note=f"{len(pending)} items queued; run with "
             f"credentials via pipeline LLM config")
        return True
    args = [PY, os.path.join(HERE, "pipeline.py"), "audit"]
    if judge:
        args.append("--judge")
    if auto_accept:
        args.append("--auto-accept")
    rc, tail = run_cmd(args)
    mark(state, "llm", "ok" if rc == 0 else "FAIL", judge=judge,
         auto_accept=auto_accept, rc=rc)
    return rc == 0


STEP_FNS = {"download": step_download, "survey": step_survey,
            "backfill": step_backfill, "audit": step_audit}


# ---------------------------------------------------------------- commands
def cmd_run(args):
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    if args.agency.strip().lower() in ("all", "*"):
        targets = [a for a in sorted(agencies()) if not a.startswith("_")]
        args.keep_going = True               # one bad agency must not stop the fleet
    else:
        targets = [a.strip().upper() for a in args.agency.split(",")]
    fleet_path = os.path.join(ROOT, "stores", "fleet_state.json")
    os.makedirs(os.path.dirname(fleet_path), exist_ok=True)

    def fleet(current, i, note=""):
        json.dump({"active": current is not None, "current": current, "index": i,
                   "total": len(targets), "steps": steps, "note": note,
                   "at": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid()},
                  open(fleet_path, "w", encoding="utf-8"))

    for i, agency in enumerate(targets, 1):
        fleet(agency, i)
        A.ensure_profile(agency)
        state = load_state(agency)
        state["running"] = True
        save_state(agency, state)
        print(f"=== {agency}: {steps}")
        try:
            for s in steps:
                state["steps"][s] = {"status": "running",
                                     "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                save_state(agency, state)
                if s == "llm":
                    ok = step_llm(agency, state, judge=args.judge,
                                  auto_accept=args.auto_accept)
                elif s == "llm-triage":
                    ok = step_llm(agency, state, triage=True)
                elif s in STEP_FNS:
                    ok = STEP_FNS[s](agency, state)
                else:
                    print(f"unknown step {s}"); ok = False
                save_state(agency, state)
                if not ok and not args.keep_going:
                    print(f"step {s} failed -- stopping {agency}")
                    break
        finally:
            state["running"] = False
            save_state(agency, state)
    fleet(None, len(targets), "done")


def cmd_status(args):
    rows = []
    for agency in sorted(agencies()):
        if agency.startswith("_"):
            continue
        if args.agency and agency != args.agency.upper():
            continue
        st = load_state(agency)
        sdir = store_dir(agency)
        ver = {}
        vp = os.path.join(sdir, "verification.json")
        if os.path.exists(vp):
            ver = json.load(open(vp, encoding="utf-8"))
        steps = {k: v["status"] for k, v in st.get("steps", {}).items()}
        rows.append((agency, steps, ver.get("rows", ""), ver.get("editions", "")))
        print(f"{agency:10} steps={steps} rows={ver.get('rows','-')} "
              f"editions={ver.get('editions','-')} "
              f"queue={ver.get('review_queue_len','-')}")
    if not rows:
        print("no agencies match")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--agency", required=True, help="agency name or comma list")
    r.add_argument("--steps", default=",".join(DEFAULT_STEPS))
    r.add_argument("--judge", action="store_true")
    r.add_argument("--auto-accept", action="store_true")
    r.add_argument("--keep-going", action="store_true")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("status")
    s.add_argument("--agency", default="")
    s.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
