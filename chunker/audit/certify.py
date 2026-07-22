"""Certificate assembly — the honest reg-only audit + the companion certificate (Decision A).

Ports orchestrator.step_audit and adds the COMPANION_DOCS §8 split:
  * regulation_certs: newest edition of each era, chunk, audit the REGULATION rows with
    companions EXCLUDED from the denominator (classify_companion + the proven archive regex,
    both in exclude mode) -> honest reg-only covered% + classifier-free section_completeness.
  * companion_summary: summarize stores/<AG>/companion.json -- rows / editions / doc_class
    counts / invariants / dates, plus available-vs-captured companion counts (its own
    completeness measure) -- the numbers the dashboard companion line renders (§11).

Read-only; returns the summary dict, the caller persists it (into state.json in Phase F).
"""
import os
import json
import time
from collections import Counter

from chunker.store import Store
from chunker.parsers import _adapter as A
from chunker.audit import corpus_audit as CU
from chunker import paths
from chunker import dates as dates_mod

# proven archive companion detection (the orchestrator's ERA regex), union'd with
# classify_companion in exclude mode so the reg denominator is companion-free.
_ARCHIVE_COMPANION_RX = r"(?i)^(MP|IG)[\d_]|^pgi[\d_.-]|attachment|^\d{2}-\d|^annex"
# folders shipping multiple renders: audit only the render the parser reads
ERA_FILES_RX = {"agov": r"(?i)AFARS[-_]PART[-_]\d+\.html$",
                "ditaot": r"(FAR|AFARS)[-_](Part|PART)[-_]\d+\.html$",
                "dita": r"(FAR|AFARS)[-_](Part|PART)[-_]\d+\.html$"}

# Declared "sections" that are deliberately NOT chunked as prose rows -- known exceptions,
# not drops. FAR 52.301 is the Part 52 clause MATRIX (a cross-reference table); its
# applicability data belongs distributed on the clause rows, and a STRUCTURED parse is a
# deferred enhancement (docs/REFERENCE_ENGINE_EXTENSION.md). So it is exempt from `missing`.
KNOWN_NON_SECTIONS = {("FAR", "52.301")}


def _eras_cache_path(agency):
    for base in (paths.CACHE_DIR, os.path.join(paths.ROOT, "pipeline", "cache")):
        p = os.path.join(base, "archive_eras.json" if agency == "FAR"
                         else f"{agency.lower()}_eras.json")
        if os.path.exists(p):
            return p
    return ""


def _newest_per_era(archives_dir, eras_cache_path):
    picks = []
    if eras_cache_path and os.path.exists(eras_cache_path):
        m = json.load(open(eras_cache_path, encoding="utf-8"))
        by_era = {}
        for folder, v in m.items():
            if v.get("era") in A.ERA_CHUNKERS and v.get("effective_date"):
                by_era.setdefault(v["era"], []).append((v["effective_date"], folder))
        for era, lst in by_era.items():
            picks.append((era, sorted(lst)[-1][1]))
    return picks


def regulation_certs(agency, archives_dir, eras_cache_path):
    """Per-era certs over the newest archive edition, companions EXCLUDED from the
    denominator (honest reg-only covered%). Returns (certs, review_queue)."""
    certs, queue = [], []
    for era, folder in _newest_per_era(archives_dir, eras_cache_path):
        edir = os.path.join(archives_dir, folder)
        if not os.path.isdir(edir):
            continue
        cfg = A.default_cfg(agency)
        cfg["source_version"] = folder
        cfg["pipeline_version"] = "audit"
        try:
            rows, _ = A.ERA_CHUNKERS[era](edir, cfg, {})
            src = A._dtt_files(edir) if era == "ditaot-topics" else None
            rep = CU.audit(edir, rows, files_rx=ERA_FILES_RX.get(era), source_files=src,
                           companion_filter="exclude",
                           companion_rx=_ARCHIVE_COMPANION_RX, companion_rx_mode="exclude")
        except Exception as e:
            certs.append({"era": era, "folder": folder, "error": repr(e)[:200]})
            continue
        t = rep["totals"]
        u = t["residue"].get("UNCLASSIFIED", 0)
        pct = 100 * u / max(t["source_chars"], 1)
        cert = {"era": era, "folder": folder, "source_chars": t["source_chars"],
                "covered_pct": round(100 * t["covered_chars"] / max(t["source_chars"], 1), 2),
                "accounted_pct": round(100 - pct, 3), "residue": t["residue"],
                "unclassified_chars": u, "unclassified_pct": round(pct, 3),
                "samples": [s["text"][:200] for s in rep["unclassified_samples"][:5]],
                "pass": pct <= 0.5}
        if era != "dita":
            try:
                mc = CU.section_completeness(edir, rows, era)
                exempt = [m for m in mc["missing"] if (agency, m) in KNOWN_NON_SECTIONS]
                missing = [m for m in mc["missing"] if (agency, m) not in KNOWN_NON_SECTIONS]
                cert["missing_sections"] = missing[:40]
                cert["missing_count"] = len(missing)
                cert["publisher_sections"] = mc["publisher_with_body"]
                if exempt:
                    cert["known_exceptions"] = exempt        # e.g. FAR 52.301 matrix -- not a drop
                if missing:
                    cert["pass"] = False
            except Exception as e:
                cert["missing_error"] = repr(e)[:150]
        certs.append(cert)
        for s in rep["unclassified_samples"]:
            queue.append({"era": era, "folder": folder, "kind": "residue", **s,
                          "status": "pending"})
    return certs, queue


def _available_companions(agency):
    """Honest companion denominators from the current GitHub HEAD:
      body           {doc_class: n} BODY-BEARING companion units the chunker actually yields
                     (capture on) -- excludes empty containers, so it's the right completeness
                     denominator (not a raw filename count);
      out_of_ditamap {doc_class: n} companion-named files NOT referenced by the ditamap -- a
                     known uncaptured gap (e.g. DFARS Appendix-id* files), reported separately.
    Chunks HEAD once."""
    from chunker.ingest import canon as fc
    import glob as _g
    repo = fc.repo_dir(agency)
    if agency in fc.NO_REPO or not os.path.isdir(os.path.join(repo, ".git")):
        return {}, {}
    cfg = A.default_cfg(agency)
    cfg["pipeline_version"] = "audit"
    dm = fc._ditamap(agency)
    if dm:
        cfg["ditamap"] = dm
    cfg["capture_companions"] = True
    try:
        _rows, man = A.chunk_edition_canon(repo, cfg, {})
    except Exception:
        return {}, {}
    body = Counter(c["doc_class"] for c in man.get("companions", []))
    dd = A._ecfr_dita_dir(repo)
    try:
        ecfr = set(A._ecfr_files(dd, dm))
    except Exception:
        ecfr = set()
    allf = set(_g.glob(os.path.join(dd, "**", "*.dita"), recursive=True)) | \
        set(_g.glob(os.path.join(dd, "*.dita")))
    off = Counter()
    for f in allf - ecfr:
        dc = A.classify_companion(os.path.basename(f))
        if dc:
            off[dc] += 1
    return dict(body), dict(off)


def companion_summary(agency, store_dir):
    """Summarize stores/<AG>/companion.json for the dashboard companion line (§11).
    Completeness is over BODY-BEARING companion units (empty containers never count), with
    out-of-ditamap files flagged as a separate known gap."""
    cpath = os.path.join(store_dir, "companion.json")
    if not os.path.exists(cpath):
        return None
    cs = Store(store_dir, f"{agency}-COMPANION", name="companion")
    cur = [r for r in cs.rows if r["effective_to"] is None]
    captured = Counter(r.get("doc_class") for r in cur)
    available_body, out_of_ditamap = _available_companions(agency)
    problems = cs.verify()
    git_rows = [r for r in cs.rows if r.get("source") == "gsa-github"]
    classes = sorted(set(captured) | set(available_body) | set(out_of_ditamap))
    by_class = {c: {"captured": captured.get(c, 0),
                    "available_body": available_body.get(c, 0),
                    "out_of_ditamap": out_of_ditamap.get(c, 0)} for c in classes}
    tot_cap, tot_avail = sum(captured.values()), sum(available_body.values())
    return {
        "present": True,
        "rows": len(cs.rows), "editions": len(cs.editions), "current_units": len(cur),
        # completeness over body-bearing units (empty containers excluded): 100% == every
        # body-bearing companion the ditamap ships was captured
        "captured_pct": round(100 * tot_cap / tot_avail, 1) if tot_avail else None,
        "by_class": by_class,
        "out_of_ditamap_total": sum(out_of_ditamap.values()),   # known gap (e.g. DFARS)
        "invariants_ok": not problems,
        "dates_ok": (all(e.get("effective_date") for e in cs.editions)
                     and all(r.get("commit_date") for r in git_rows)),
    }


def certify(agency, store_dir=None, archives_dir=None, eras_cache_path=None):
    """Full per-agency certification: honest reg verification + companion summary."""
    store_dir = store_dir or paths.store_dir(agency)
    archives_dir = archives_dir or paths.archive_dir(agency)
    eras_cache_path = eras_cache_path or _eras_cache_path(agency)
    st = Store(store_dir, agency)
    problems = st.verify()
    certs, queue = regulation_certs(agency, archives_dir, eras_cache_path)
    real = [c for c in certs if (c.get("source_chars") or 0) > 0]
    cov = [c["covered_pct"] for c in real if isinstance(c.get("covered_pct"), (int, float))]
    acc = [c["accounted_pct"] for c in real if isinstance(c.get("accounted_pct"), (int, float))]
    verification = {
        "invariants_ok": not problems, "invariant_problems": problems[:20],
        "editions": len(st.editions), "rows": len(st.rows),
        "covered_pct": round(min(cov), 2) if cov else None,          # worst era (honest, reg-only)
        "accounted_pct": round(min(acc), 3) if acc else None,
        "missing_sections": sum(c.get("missing_count", 0) for c in certs
                                if isinstance(c.get("missing_count"), int)),
        "certificates": certs, "review_queue_len": len(queue),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {"agency": agency, "verification": verification,
            "companion": companion_summary(agency, store_dir), "review_queue": queue,
            "dates": dates_mod.date_coverage(st)}      # reuse the loaded store (no 2nd load)
