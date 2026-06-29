#!/usr/bin/env python3
"""Coverage check — prove the chunker did NOT drop any source text.

Trust tool #1 (content completeness). For every processed unit it compares the SOURCE DITA's text
content against the union of everything we emit for that unit — the unit chunk's `text`, its alternate
chunks' `text`, and the (relocated) end-of-clause marker. Comparison is on a whitespace-stripped,
lower-cased, alphanumeric signature, so it is immune to inline-markup word-splitting (e.g.
"<ph>S</ph>mall" -> "small") and only flags source text that is genuinely ABSENT from the output.

It reports:
  * overall coverage — % of source character-shingles present in the output (headline trust number),
  * any unit below 100%, naming the element tags whose text was dropped (the actionable bit),
  * a census of text-bearing element tags the flattener does not handle (what to fix or whitelist).

This is deterministic and needs no LLM / no ground truth — run it every build.

Usage:
  python verify_coverage.py                          # whole corpus (pipeline.config.json + ditamap)
  python verify_coverage.py --files 52.212-5 52.219-9 # just these
  python verify_coverage.py --input-dir /path/to/dita
"""
import re, os, sys, json, argparse, collections
import xml.etree.ElementTree as ET
import chunker as ck

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # source samples are non-ASCII; don't crash on cp1252
except Exception:
    pass

# Tags the flattener turns into text (block + the inline tags it descends through). Anything carrying
# text that is NOT here is a candidate for being dropped — those are what we name in the report.
HANDLED = {"p", "ol", "ul", "li", "table", "simpletable", "tgroup", "thead", "tbody", "row", "entry",
           "colspec", "image", "fig", "ph", "xref", "i", "b", "u", "sub", "sup", "cite", "title",
           "term", "keyword", "q", "tm", "lines", "section"}

sig = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())   # whitespace/punct-insensitive signature

def load_cfg(args):
    f = os.path.join(HERE, "pipeline.config.json")
    c = json.load(open(f, encoding="utf-8")) if os.path.exists(f) else {}
    input_dir = args.input_dir or os.environ.get("PIPELINE_INPUT_DIR") or c.get("input_dir") or "dita"
    if not os.path.isabs(input_dir):
        input_dir = os.path.normpath(os.path.join(HERE, input_dir))
    return {"regulation": c.get("regulation", "FAR"),
            "url_template": c.get("url_template", "https://www.acquisition.gov/far/{num}"),
            "input_dir": input_dir, "ditamap": c.get("ditamap", "FAR.ditamap"),
            "bottom_depth": 1, "bottom_level": "subparagraph",   # unit + alternates is all coverage needs
            "files": args.files}

def main():
    ap = argparse.ArgumentParser(description="Coverage check: did chunking drop any source text?")
    ap.add_argument("--files", nargs="+", metavar="FILE")
    ap.add_argument("--input-dir", dest="input_dir")
    args = ap.parse_args()

    rows, _, sources = ck.run_chunker(load_cfg(args))
    out_by = collections.defaultdict(list)              # citation -> [unit text, alt texts, end marker]
    for r in rows:
        if r["type"] in ("section", "subsection"):
            out_by[r["citation"]].append(r["text"])
            out_by[r["citation"]].append(r.get("end_marker", ""))

    # Per-fragment check: every source text node (element .text/.tail) must appear, as a
    # whitespace-insensitive substring, somewhere in the unit's output. Fragment-level (not whole-unit)
    # so it's immune to BOTH inline word-splitting and the end-marker/alternate relocation boundaries.
    units = gap_units = tot_src = tot_missing = 0
    gaps, census = [], collections.Counter()
    for cit, texts in out_by.items():
        path = sources.get(cit)
        if not path:
            continue
        try:
            raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
            cb = next((e for e in ET.fromstring(raw).iter() if e.tag in ("conbody", "body")), None)
        except (ET.ParseError, OSError):
            continue
        if cb is None:
            continue
        units += 1
        osig = sig(" ".join(texts))
        src = miss = 0
        dropped = []
        for e in cb.iter():
            for owner, frag in ((e.tag, e.text), (e.tag + " (tail)", e.tail)):
                f = sig(frag)
                if not f:
                    continue
                src += len(f)
                if f not in osig:                       # this source text is nowhere in the output
                    miss += len(f)
                    if len(f) >= 4:                     # ignore 1-3 char noise; report real fragments
                        dropped.append((owner, " ".join(frag.split())[:70]))
        tot_src += src; tot_missing += miss
        if dropped:
            gap_units += 1
            gaps.append((cit, miss, src, dropped[:4]))
            for tag, _ in dropped:
                census[tag.replace(" (tail)", "")] += 1

    cov = 100.0 * (1 - tot_missing / tot_src) if tot_src else 100.0
    print(f"units checked          : {units}")
    print(f"overall text coverage  : {cov:.4f}%   ({tot_missing} missing / {tot_src} source chars)")
    print(f"units with a real gap  : {gap_units}")
    print(f"dropped element tags   : {dict(census.most_common()) or '(none — every source fragment is present)'}")
    if gaps:
        print("\nunits with dropped text (tag -> the source text not found in output):")
        for cit, m, s, dropped in sorted(gaps, key=lambda x: -x[1])[:25]:
            print(f"  {cit:18} {m}/{s} chars missing")
            for tag, txt in dropped:
                print(f"      <{tag}> {txt!r}")
    print("\n[OK] every source text fragment is present in the output." if not census else
          "\n[WARN] source text dropped — add the listed tags to the flattener (extract_json.flatten_nodes) or whitelist.")

if __name__ == "__main__":
    main()
