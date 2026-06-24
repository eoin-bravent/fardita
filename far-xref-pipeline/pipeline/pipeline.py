#!/usr/bin/env python3
"""One pipeline, two commands around the human review step:

  python pipeline.py run    [--config C] [--mock-llm F | --no-llm] [--limit N]
      resolve file set -> chunk -> manifest -> blind LLM audit -> reconcile -> review.html
  python pipeline.py apply  [--config C] --decisions decisions.json
      merge approved refs -> <regulation>_verified.json   (with provenance)

Config: pipeline.config.json (regulation, input_dir, bottom_level, gemini model/reasoning, …).
Secret: GEMINI_API_KEY in the environment (never written to config or logs).
"""
import os, sys, json, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chunker, reconcile, review

DITA_DEFAULT = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULTS = {
    "regulation": "FAR",
    "input_dir": DITA_DEFAULT,
    "bottom_level": "paragraph",
    "url_template": "https://www.acquisition.gov/far/{num}",
    "output_dir": os.path.join(HERE, "out"),
    # LLM backend: "usai" (default, stdlib REST) or "vertex" (Google Vertex AI via google-genai).
    "provider": "usai",
    # USAi.gov (OpenAI-compatible). base_url is agency-specific (https://<agency>.usai.gov).
    "gemini": {"model": "gemini-2.5-pro", "base_url": "", "reasoning": True,
               "thinking_budget": -1, "judge": False},
    # Vertex AI (used only when provider == "vertex"). Auth via GOOGLE_APPLICATION_CREDENTIALS.
    "vertex": {"project": "", "location": ""},
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
        cfg.update({k: v for k, v in user.items() if k not in ("gemini", "vertex")})
        cfg["gemini"].update(user.get("gemini", {}))
        cfg["vertex"].update(user.get("vertex", {}))
    # env / .env overlay
    for ev, k in {"PIPELINE_REGULATION": "regulation", "PIPELINE_INPUT_DIR": "input_dir",
                  "PIPELINE_BOTTOM_LEVEL": "bottom_level", "PIPELINE_OUTPUT_DIR": "output_dir"}.items():
        if os.environ.get(ev):
            cfg[k] = os.environ[ev]
    if os.environ.get("LLM_PROVIDER"):
        cfg["provider"] = os.environ["LLM_PROVIDER"]
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
    for attr in ("regulation", "input_dir", "bottom_level", "output_dir", "provider"):
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

def cmd_run(cfg, args):
    out = cfg["output_dir"]; reg = cfg["regulation"]
    print("chunking…")
    rows, manifest, sources = chunker.run_chunker(cfg)

    if getattr(args, "dump_payload", None):
        import gemini_audit
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
    if args.mock_llm:
        llm = json.load(open(args.mock_llm, encoding="utf-8"))
    else:
        gemini_audit = _audit_backend(cfg)
        print(f"auditing {len(units)} units with {cfg['gemini']['model']} via {cfg['provider']} "
              f"(reasoning={cfg['gemini']['reasoning']})…")
        llm = gemini_audit.audit(units, cfg, os.path.join(out, "llm_cache"))

    addr = reconcile.build_address_map(rows)
    queue, stats, confirmed = reconcile.reconcile(rows, llm, addr)

    # optional LLM judge: pre-fill each discrepancy with a recommendation + rationale
    if cfg["gemini"].get("judge") and queue and not args.mock_llm:
        gemini_audit = _audit_backend(cfg)
        from collections import defaultdict
        raw_by_cit = dict(units)
        by_unit = defaultdict(list)
        for i, it in enumerate(queue):
            by_unit[it["unit"]].append(i)
        print(f"judging {len(by_unit)} units with discrepancies…")
        for ucit, idxs in by_unit.items():
            discr = [{"n": i, "parser": queue[i]["parser"], "llm": queue[i]["llm"],
                      "bucket": queue[i]["bucket"]} for i in idxs]
            recs = gemini_audit.judge(ucit, raw_by_cit.get(ucit, ""), discr, cfg,
                                      os.path.join(out, "llm_cache"))
            for i in idxs:
                if i in recs:
                    queue[i]["judge"] = {"choice": recs[i].get("choice"),
                                         "value": recs[i].get("value", []),
                                         "rationale": recs[i].get("rationale", "")}

    json.dump(queue, open(os.path.join(out, f"{reg}_queue.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    json.dump(confirmed, open(os.path.join(out, f"{reg}_confirmed.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    review.write_review(queue, os.path.join(out, f"{reg}_review.html"),
                        f"{reg} cross-reference review")
    print(f"  reconcile: {stats}")
    print(f"  review page: {os.path.join(out, f'{reg}_review.html')}  ({len(queue)} items to review)")

def cmd_apply(cfg, args):
    out = cfg["output_dir"]; reg = cfg["regulation"]
    rows = json.load(open(os.path.join(out, f"{reg}_chunks.json"), encoding="utf-8"))
    cpath = os.path.join(out, f"{reg}_confirmed.json")
    confirmed = json.load(open(cpath, encoding="utf-8")) if os.path.exists(cpath) else {}
    # merge one or more decisions files; later files win per (unit, llm_target)
    merged = {}
    for p in (args.decisions or []):
        for d in json.load(open(p, encoding="utf-8")):
            merged[(d["unit"], d.get("llm_target", str(d.get("value"))))] = d
    decisions = list(merged.values())

    # 1) annotate every existing (parser) ref with provenance
    for r in rows:
        conf = set(confirmed.get(r["citation"], []))
        for cr in r["cross_references"]:
            corrob = reconcile.norm_cit(cr["target"]) in conf
            cr["provenance"] = {"producer": "parser+gemini" if corrob else "parser",
                                "status": "corroborated" if corrob else "parser_only"}

    # 2) append human-approved additions to their unit row
    by_cit = {r["citation"]: r for r in rows}
    added = 0
    for d in decisions:
        if d["choice"] == "reject" or not d.get("value"):
            continue
        u = by_cit.get(d["unit"])
        if not u:
            continue
        producer = {"llm": "gemini+human", "manual": "human", "parser": "parser+human"}[d["choice"]]
        for tgt in d["value"]:
            if any(reconcile.norm_cit(c["target"]) == reconcile.norm_cit(tgt)
                   for c in u["cross_references"]):
                continue                                  # already present
            u["cross_references"].append({
                "target": tgt, "confidence": "inferred",
                "mentions": [{"kind": "inferred", "context": "(human review)"}],
                "provenance": {"producer": producer, "status": "human_approved"}})
            added += 1

    path = os.path.join(out, f"{reg}_verified.json")
    json.dump(rows, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"wrote {path}  (+{added} human-approved refs, {len(decisions)} decisions)")

def add_overrides(p):
    """Config-overriding flags shared by both subcommands (highest precedence)."""
    p.add_argument("--config")
    p.add_argument("--regulation"); p.add_argument("--input-dir", dest="input_dir")
    p.add_argument("--bottom-level", dest="bottom_level"); p.add_argument("--output-dir", dest="output_dir")
    p.add_argument("--provider", choices=["usai", "vertex"], help="LLM backend (default usai)")
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
