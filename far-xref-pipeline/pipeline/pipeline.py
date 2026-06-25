#!/usr/bin/env python3
"""One pipeline, two commands around the human review step:

  python pipeline.py run    [--config C] [--mock-llm F | --no-llm] [--limit N]
      resolve file set -> chunk -> manifest -> blind LLM audit -> reconcile -> review.html
  python pipeline.py apply  [--config C] --decisions decisions.json
      merge approved refs -> <regulation>_verified.json   (every ref tagged with a status)

Config: pipeline.config.json (regulation, input_dir, bottom_level, gemini model/reasoning, …).
Secret: GEMINI_API_KEY in the environment (never written to config or logs).
"""
import os, sys, json, time, glob, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chunker, reconcile, review, gemini_audit
import extract_json as X                          # parse_external (for human-corrected external citations)

DITA_DEFAULT = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULTS = {
    "regulation": "FAR",
    "input_dir": DITA_DEFAULT,
    "bottom_level": "paragraph",
    "url_template": "https://www.acquisition.gov/far/{num}",
    "output_dir": os.path.join(HERE, "out"),
    # LLM backend: "usai" (default, stdlib REST) or "vertex" (Google Vertex AI via google-genai).
    "provider": "usai",
    "concurrency": 8,                                     # parallel LLM calls per run (1 = sequential)
    # USAi.gov (OpenAI-compatible). base_url is agency-specific (https://<agency>.usai.gov).
    "gemini": {"model": "gemini-2.5-pro", "base_url": "", "reasoning": True,
               "thinking_budget": -1, "judge": False},
    # Vertex AI (used only when provider == "vertex"). Auth via GOOGLE_APPLICATION_CREDENTIALS.
    "vertex": {"project": "", "location": ""},
    # Token pricing for the cost estimate — public Gemini 2.5 Pro rates (≤200k ctx). Thinking tokens
    # bill at the output rate. Edit here or in pipeline.config.json; set 0 to hide the dollar figure.
    "pricing": {"input_per_1m": 1.25, "output_per_1m": 10.0, "currency": "USD"},
}

def _audit_backend(cfg):
    """Pick the LLM backend module by provider. Both expose audit()/judge() identically."""
    provider = (cfg.get("provider") or "usai").lower()
    if provider in ("vertex", "vertexai", "gcp"):
        import vertex_audit
        return vertex_audit
    import gemini_audit
    return gemini_audit

def load_dotenv(path):
    """Load KEY=VALUE lines into the environment (real env always wins)."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def load_config(args):
    """Precedence (low -> high): DEFAULTS < pipeline.config.json < .env/env < CLI flags."""
    cfg = json.loads(json.dumps(DEFAULTS))
    path = getattr(args, "config", None) or os.path.join(HERE, "pipeline.config.json")
    if os.path.exists(path):
        user = json.load(open(path, encoding="utf-8"))
        cfg.update({k: v for k, v in user.items() if k not in ("gemini", "vertex", "pricing")})
        cfg["gemini"].update(user.get("gemini", {}))
        cfg["vertex"].update(user.get("vertex", {}))
        cfg["pricing"].update(user.get("pricing", {}))
    # env / .env overlay
    for ev, k in {"PIPELINE_REGULATION": "regulation", "PIPELINE_INPUT_DIR": "input_dir",
                  "PIPELINE_BOTTOM_LEVEL": "bottom_level", "PIPELINE_OUTPUT_DIR": "output_dir"}.items():
        if os.environ.get(ev):
            cfg[k] = os.environ[ev]
    if os.environ.get("LLM_PROVIDER"):
        cfg["provider"] = os.environ["LLM_PROVIDER"]
    if os.environ.get("LLM_CONCURRENCY"):
        cfg["concurrency"] = int(os.environ["LLM_CONCURRENCY"])
    if os.environ.get("GEMINI_MODEL"):
        cfg["gemini"]["model"] = os.environ["GEMINI_MODEL"]
    for ev, k in {"VERTEX_PROJECT": "project", "GOOGLE_CLOUD_PROJECT": "project",
                  "VERTEX_LOCATION": "location", "GOOGLE_CLOUD_LOCATION": "location"}.items():
        if os.environ.get(ev):
            cfg["vertex"][k] = os.environ[ev]
    if os.environ.get("USAI_BASE_URL") or os.environ.get("GEMINI_BASE_URL"):
        cfg["gemini"]["base_url"] = os.environ.get("USAI_BASE_URL") or os.environ["GEMINI_BASE_URL"]
    if os.environ.get("GEMINI_REASONING"):
        cfg["gemini"]["reasoning"] = os.environ["GEMINI_REASONING"].lower() in ("1", "true", "yes", "on")
    if os.environ.get("GEMINI_THINKING_BUDGET"):
        cfg["gemini"]["thinking_budget"] = int(os.environ["GEMINI_THINKING_BUDGET"])
    if os.environ.get("GEMINI_JUDGE"):
        cfg["gemini"]["judge"] = os.environ["GEMINI_JUDGE"].lower() in ("1", "true", "yes", "on")
    # CLI overlay (highest precedence)
    for attr in ("regulation", "input_dir", "bottom_level", "output_dir", "provider", "concurrency"):
        if getattr(args, attr, None) is not None:
            cfg[attr] = getattr(args, attr)
    if getattr(args, "model", None):
        cfg["gemini"]["model"] = args.model
    if getattr(args, "reasoning", None) is not None:
        cfg["gemini"]["reasoning"] = args.reasoning
    if getattr(args, "thinking_budget", None) is not None:
        cfg["gemini"]["thinking_budget"] = args.thinking_budget
    if getattr(args, "judge", None) is not None:
        cfg["gemini"]["judge"] = args.judge
    if getattr(args, "files", None):
        cfg["files"] = args.files                         # run only these .dita files (names or paths)
    # finalize
    if cfg["bottom_level"] not in chunker.LEVEL_DEPTH:
        sys.exit(f"bottom_level must be one of {list(chunker.LEVEL_DEPTH)}")
    cfg["bottom_depth"] = chunker.LEVEL_DEPTH[cfg["bottom_level"]]
    for k in ("input_dir", "output_dir"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.abspath(os.path.join(HERE, cfg[k]))
    os.makedirs(cfg["output_dir"], exist_ok=True)
    return cfg

def _corpus_address_map(cfg, out, reg):
    """Address map over the WHOLE corpus so --files subset runs still validate cross-file targets.
    Cached to out/<REG>_addrmap.json, keyed by .dita file count."""
    n_files = len(glob.glob(os.path.join(cfg["input_dir"], "*.dita")))
    p = os.path.join(out, f"{reg}_addrmap.json")
    if os.path.exists(p):
        c = json.load(open(p, encoding="utf-8"))
        if c.get("n_files") == n_files:
            return set(c["map"])
    full = dict(cfg); full.pop("files", None)             # chunk the whole folder, not just --files
    print(f"  building corpus address map ({n_files} files)…")
    crows, _, _ = chunker.run_chunker(full)
    m = reconcile.build_address_map(crows)
    json.dump({"n_files": n_files, "map": sorted(m)}, open(p, "w", encoding="utf-8"))
    return m

def _cost(tokens, pricing):
    inr, outr = pricing.get("input_per_1m", 0) or 0, pricing.get("output_per_1m", 0) or 0
    c = lambda d: round(d["prompt"] / 1e6 * inr + (d["thinking"] + d["output"]) / 1e6 * outr, 4)
    tot = tokens["total"]
    return {"currency": pricing.get("currency", "USD"), "rates_per_1m": {"input": inr, "output": outr},
            "audit": c(tokens["audit"]), "judge": c(tokens["judge"]), "total": c(tot),
            "in_cost": round(tot["prompt"] / 1e6 * inr, 4),
            "out_cost": round((tot["thinking"] + tot["output"]) / 1e6 * outr, 4)}

def _run_summary(cfg, stats, timing, tokens, n_units, mock):
    return {"provider": cfg["provider"], "model": cfg["gemini"]["model"],
            "concurrency": cfg["concurrency"], "units": n_units,
            "cache_hits": 0 if mock else max(0, n_units - tokens["audit"]["calls"]),
            "status_counts": stats, "timing_sec": {k: round(v, 1) for k, v in timing.items()},
            "tokens": tokens, "cost": _cost(tokens, cfg["pricing"])}

def _print_summary(s):
    print("  timing(s): " + "  ".join(f"{k}={v}" for k, v in s["timing_sec"].items()))
    tk = s["tokens"]
    if not tk["total"]["calls"]:
        print("  tokens: none recorded (mock-llm or fully cached)")
        return
    for st in ("audit", "judge"):
        d = tk[st]
        if d["calls"]:
            print(f"  tokens {st}: {d['calls']} calls  in {d['prompt']:,}  thinking {d['thinking']:,}"
                  f"  out {d['output']:,}  total {d['total']:,}")
    print(f"  tokens TOTAL: {tk['total']['total']:,}  (cache hits this run: {s['cache_hits']})")
    if tk["total"]["reported"] < tk["total"]["calls"]:
        print(f"  note: {tk['total']['calls'] - tk['total']['reported']} call(s) returned no usage data")
    co = s.get("cost", {})
    if tk["total"]["calls"] and (co.get("rates_per_1m", {}).get("input") or co.get("rates_per_1m", {}).get("output")):
        r = co["rates_per_1m"]
        print(f"  est. cost: {co['currency']} {co['total']:.4f}  (in {co['in_cost']:.4f} + out {co['out_cost']:.4f}"
              f"; rates {r['input']}/{r['output']} per 1M — set in pipeline.config.json 'pricing')")

def cmd_run(cfg, args):
    out = cfg["output_dir"]; reg = cfg["regulation"]
    t, t0 = {}, time.perf_counter()
    print("chunking…")
    rows, manifest, sources = chunker.run_chunker(cfg)
    t["chunk"] = time.perf_counter() - t0

    if getattr(args, "dump_payload", None):
        cit = args.dump_payload
        key = next((c for c in sources if c == cit or c == f"{reg}-{cit}"), None)
        if not key:
            sys.exit(f"unit {cit} not found (try the full citation, e.g. {reg}-{cit})")
        raw = open(sources[key], encoding="utf-8").read()
        print("=" * 70 + "\nSYSTEM INSTRUCTION:\n" + "=" * 70)
        print(gemini_audit.AUDIT_SYSTEM.format(regulation=reg, citation=key))
        print("\n" + "=" * 70 + f"\nUSER CONTENT (entire raw .dita for {key}, {len(raw)} chars):\n" + "=" * 70)
        print(raw)
        return

    json.dump(rows, open(os.path.join(out, f"{reg}_chunks.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    json.dump(manifest, open(os.path.join(out, f"{reg}_manifest.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    print(f"  rows: {len(rows)}   processed {manifest['processed_count']}"
          f"   skipped {manifest['skipped_count']}")

    if args.no_llm and not args.mock_llm:                 # parser-only: just chunks + manifest
        print("  parser-only (--no-llm): wrote chunks + manifest; skipped audit / reconcile / review")
        return

    # LLM path: each call gets the prompt + the ENTIRE raw .dita file for that unit
    units = [(r["citation"], open(sources[r["citation"]], encoding="utf-8").read())
             for r in rows if r["type"] in ("section", "subsection") and r["citation"] in sources]
    if args.limit:
        units = units[:args.limit]
    gemini_audit.TRACKER.reset()
    t1 = time.perf_counter()
    if args.mock_llm:
        llm, backend = json.load(open(args.mock_llm, encoding="utf-8")), None
    else:
        backend = _audit_backend(cfg)
        print(f"auditing {len(units)} units with {cfg['gemini']['model']} via {cfg['provider']} "
              f"(reasoning={cfg['gemini']['reasoning']}, concurrency={cfg['concurrency']})…")
        llm = backend.audit(units, cfg, os.path.join(out, "llm_cache"))
    t["audit"] = time.perf_counter() - t1

    addr = (_corpus_address_map(cfg, out, reg) if cfg.get("files")
            else reconcile.build_address_map(rows))
    t2 = time.perf_counter()
    ledger, stats = reconcile.reconcile(rows, llm, addr)
    t["reconcile"] = time.perf_counter() - t2

    # optional LLM judge (concurrent): recommend accept/reject/manual per disagreement
    t["judge"] = 0.0
    if cfg["gemini"].get("judge") and backend is not None:
        review_idx = [i for i, it in enumerate(ledger)
                       if it["needs_review"] and it.get("scope") == "internal"]   # judge prompt is FAR-internal
        if review_idx:
            from collections import defaultdict
            raw_by_cit = dict(units)
            by_unit = defaultdict(list)
            for i in review_idx:
                by_unit[ledger[i]["unit"]].append(i)
            jobs = [(ucit, raw_by_cit.get(ucit, ""),
                     [{"n": i, "target": ledger[i]["target"],
                       "source": "parser" if ledger[i]["status"] == "parser_inferred" else "llm",
                       "evidence": (ledger[i]["parser"] or ledger[i]["llm"] or {}).get("evidence", "")}
                      for i in idxs])
                    for ucit, idxs in by_unit.items()]
            print(f"judging {len(jobs)} units with disagreements (concurrency={cfg['concurrency']})…")
            t3 = time.perf_counter()
            recs_by_unit = backend.judge_all(jobs, cfg, os.path.join(out, "llm_cache"))
            t["judge"] = time.perf_counter() - t3
            for i in review_idx:
                recs = recs_by_unit.get(ledger[i]["unit"], {})
                if i in recs:
                    ledger[i]["judge"] = {"choice": recs[i].get("choice"),
                                          "value": recs[i].get("value", []),
                                          "rationale": recs[i].get("rationale", "")}

    t["total"] = time.perf_counter() - t0
    summary = _run_summary(cfg, stats, t, gemini_audit.TRACKER.summary(), len(units), bool(args.mock_llm))
    json.dump(ledger, open(os.path.join(out, f"{reg}_ledger.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    json.dump(summary, open(os.path.join(out, f"{reg}_token_usage.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    review.write_review(ledger, os.path.join(out, f"{reg}_review.html"),
                        f"{reg} cross-reference review", summary)
    n_review = sum(1 for it in ledger if it["needs_review"])
    print(f"  reconcile: {stats}")
    _print_summary(summary)
    print(f"  review page: {os.path.join(out, f'{reg}_review.html')}  "
          f"({len(ledger)} refs, {n_review} need review)")

def cmd_apply(cfg, args):
    out = cfg["output_dir"]; reg = cfg["regulation"]
    rows = json.load(open(os.path.join(out, f"{reg}_chunks.json"), encoding="utf-8"))
    lpath = os.path.join(out, f"{reg}_ledger.json")
    ledger = json.load(open(lpath, encoding="utf-8")) if os.path.exists(lpath) else []
    int_conf, ext_conf, ext_index = {}, {}, {}            # corroborated sets + external-item index
    for it in ledger:
        if it.get("scope", "internal") == "external":
            ekey = (it["unit"], it["target"], it.get("locator", ""))
            ext_index[ekey] = it
            if it["status"] == "corroborated":
                ext_conf.setdefault(it["unit"], set()).add((it["target"], it.get("locator", "")))
        elif it["status"] == "corroborated":
            int_conf.setdefault(it["unit"], set()).add(reconcile.norm_cit(it["target"]))

    # merge one or more decisions files; later files win per (unit, scope, target, locator)
    merged = {}
    for p in (args.decisions or []):
        for d in json.load(open(p, encoding="utf-8")):
            sc = d.get("scope", "internal")
            tk = d["target"] if sc == "external" else reconcile.norm_cit(d.get("target", ""))
            merged[(d["unit"], sc, tk, d.get("locator", ""))] = d
    decisions = list(merged.values())
    int_dec = [d for d in decisions if d.get("scope", "internal") != "external"]
    ext_dec = [d for d in decisions if d.get("scope") == "external"]
    # reject removes a ref; manual replaces it with corrected citation(s) -> both drop the original
    int_replaced = {(d["unit"], reconcile.norm_cit(d["target"])) for d in int_dec if d["choice"] in ("reject", "manual")}
    ext_replaced = {(d["unit"], d["target"], d.get("locator", "")) for d in ext_dec if d["choice"] in ("reject", "manual")}

    # 1) tag existing (parser) refs with status; drop any the human rejected/replaced
    removed = 0
    by_cit = {}
    for r in rows:
        by_cit[r["citation"]] = r
        iconf = int_conf.get(r["citation"], set())
        kept = []
        for cr in r["cross_references"]:
            t = reconcile.norm_cit(cr["target"])
            if (r["citation"], t) in int_replaced:
                removed += 1
                continue
            cr["status"] = "corroborated" if t in iconf else "parser_only"
            kept.append(cr)
        r["cross_references"] = kept
        econf = ext_conf.get(r["citation"], set())
        ekept = []
        for cr in r.get("external_references", []):
            k = (cr["target"], cr.get("locator", ""))
            if (r["citation"], cr["target"], cr.get("locator", "")) in ext_replaced:
                removed += 1
                continue
            cr["status"] = "corroborated" if k in econf else "parser_only"
            ekept.append(cr)
        if "external_references" in r:
            r["external_references"] = ekept

    # 2) append human-approved additions to their unit row
    added = 0
    for d in int_dec:                                     # internal: accepted llm-only/added + manual
        if d["choice"] == "manual":
            tgts = d.get("value", [])
        elif d["choice"] == "accept" and d.get("status") in ("llm_only", "added"):
            tgts = [d["target"]]
        else:
            continue
        u = by_cit.get(d["unit"])
        if not u:
            continue
        for tgt in tgts:
            if any(reconcile.norm_cit(c["target"]) == reconcile.norm_cit(tgt) for c in u["cross_references"]):
                continue
            u["cross_references"].append({
                "target": tgt, "confidence": "inferred",
                "mentions": [{"kind": "inferred", "evidence": "(human review)"}],
                "status": "human_approved"})
            added += 1
    for d in ext_dec:                                     # external: accepted llm-only + manual corrections
        u = by_cit.get(d["unit"])
        if not u:
            continue
        if d["choice"] == "accept" and d.get("status") == "llm_only":
            it = ext_index.get((d["unit"], d["target"], d.get("locator", "")))
            edges = [it] if it else []
        elif d["choice"] == "manual":
            edges = [X.parse_external(v) or {"ref_type": "other", "target": "other:" + v,
                                             "locator": "", "division_levels": [], "citation": v}
                     for v in d.get("value", [])]
        else:
            continue
        u.setdefault("external_references", [])
        for e in edges:
            if any(c["target"] == e["target"] and c.get("locator", "") == e.get("locator", "")
                   for c in u["external_references"]):
                continue
            u["external_references"].append({
                "target": e["target"], "ref_type": e["ref_type"], "locator": e.get("locator", ""),
                "division_levels": e.get("division_levels", []), "citation": e.get("citation", ""),
                "mentions": [{"kind": "inferred", "evidence": "(human review)"}],
                "status": "human_approved"})
            added += 1

    path = os.path.join(out, f"{reg}_verified.json")
    json.dump(rows, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"wrote {path}  (+{added} approved, -{removed} rejected/replaced, {len(decisions)} decisions)")

def add_overrides(p):
    """Config-overriding flags shared by both subcommands (highest precedence)."""
    p.add_argument("--config")
    p.add_argument("--regulation"); p.add_argument("--input-dir", dest="input_dir")
    p.add_argument("--bottom-level", dest="bottom_level"); p.add_argument("--output-dir", dest="output_dir")
    p.add_argument("--provider", choices=["usai", "vertex"], help="LLM backend (default usai)")
    p.add_argument("--concurrency", type=int, help="parallel LLM calls per run (default 8; 1 = sequential)")
    p.add_argument("--model")
    p.add_argument("--reasoning", dest="reasoning", action="store_true", default=None)
    p.add_argument("--no-reasoning", dest="reasoning", action="store_false")
    p.add_argument("--thinking-budget", dest="thinking_budget", type=int)
    p.add_argument("--judge", dest="judge", action="store_true", default=None)
    p.add_argument("--no-judge", dest="judge", action="store_false")

def main():
    load_dotenv(os.path.join(HERE, ".env"))
    ap = argparse.ArgumentParser(description="FAR ingestion pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); add_overrides(r)
    r.add_argument("--files", nargs="+", metavar="FILE",
                   help="run only these .dita files (names like 5.101 or paths) instead of the whole folder")
    r.add_argument("--mock-llm"); r.add_argument("--no-llm", action="store_true")
    r.add_argument("--limit", type=int)
    r.add_argument("--dump-payload", metavar="CITATION",
                   help="print the exact prompt + raw .dita sent for one unit, then exit")
    a = sub.add_parser("apply"); add_overrides(a)
    a.add_argument("--decisions", nargs="+", required=True, metavar="FILE",
                   help="one or more decisions.json files; later files override earlier per (unit, target)")
    args = ap.parse_args()
    cfg = load_config(args)
    (cmd_run if args.cmd == "run" else cmd_apply)(cfg, args)

if __name__ == "__main__":
    main()
