#!/usr/bin/env python3
"""Demo + field guide for the FAR JSON dataset.

Prints what EVERY field in a chunk record means, annotated against a real record, then walks the
reference / change sub-structures and a few special records (an alternate, a reference-to-alternate,
a record with an image, a reserved placeholder).

Reads `out/<REG>_verified.json` if present (so it can show the verified `status` on each reference);
otherwise it parses a small demo set live from the DITA source, so it runs with no prior run and no
API key (those records just won't carry `status`).

Usage:
  python demo_fields.py
  python demo_fields.py --file out/FAR_verified.json
"""
import os, re, sys, json, argparse, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # FAR text + glyphs are non-ASCII; don't crash on cp1252
except Exception:
    pass

CHUNK_FIELDS = {
    "citation":        "Unique id, regulation-prefixed (FAR-52.212-5). A clause and its alternates SHARE this id.",
    "regulation":      "Which regulation set (FAR / DFARS / …) — keeps ids unique across sets.",
    "type":            "STRUCTURAL level (FAR 1.105-2): section / subsection / paragraph / subparagraph.",
    "instrument":      "FUNCTIONAL kind: clause / provision / '' (ordinary regulatory section). Same for every chunk of an instrument.",
    "alternate":       "VARIANT: '' = base text; '1'/'2'/… = a clause Alternate. (citation, alternate) is the record's identity.",
    "source_version":  "FAR edition this came from — the ditamap rev (e.g. 'FAC 2026-01 March 13, 2026').",
    "pipeline_version":"git short SHA of the code that produced the record (provenance).",
    "part":            "Citation decomposed: part number.",
    "subpart":         "Citation decomposed: subpart.",
    "section":         "Citation decomposed: section.",
    "subsection":      "Citation decomposed: subsection (the number after the dash).",
    "paragraph":       "Citation decomposed: paragraph (a)/(b)/… ('' on a unit row).",
    "subparagraph":    "Citation decomposed: subparagraph (1)/(2)/… ('' unless this chunk is that deep).",
    "url":             "Source page on acquisition.gov.",
    "date":            "Effective date of this clause version (base or alternate); '' for ordinary sections/paragraphs.",
    "prescribed_by":   "FAR section that prescribes this clause/alternate ('47.507(a)'); '' where there is none.",
    "reserved":        "true when the chunk is just a '[Reserved]' placeholder (no substantive text).",
    "end_marker":      "Clause/provision terminator '(End of clause)'/'(End of provision)' — base unit only; '' elsewhere. (Stripped from `text`.)",
    "images":          "Deduped image ids in the chunk; each appears inline in `text` as '[IMAGE: id]'.",
    "changes":         "This FAC's rev-marked edits that fall inside the chunk (the change track); [] if unchanged.",
    "cross_references":"References to OTHER parts of this regulation (FAR→FAR). See sub-fields below.",
    "external_references":"References to other-government documents (U.S.C./CFR/E.O./Pub.L./OMB/forms/URLs).",
    "text":            "The chunk's text. Tables are inlined as HTML; images as '[IMAGE: id]' placeholders.",
}
XREF_FIELDS = {
    "target":     "The cited FAR citation, bare ('5.202(a)(2)'). Ranges are pre-expanded into atomic members.",
    "alternate":  "'' for a normal reference, or an arabic id when the reference is to a clause Alternate. (target, alternate) is the edge.",
    "confidence": "explicit (a tagged <xref>) or inferred (resolved from prose / a range) — the roll-up of the mentions.",
    "status":     "How it was verified: corroborated (parser+LLM) / parser_only / human_approved / auto_accepted. (verified.json only)",
    "mentions":   "Every occurrence of this reference — list of {kind, evidence}.",
}
MENTION_FIELDS = {
    "kind":     "Per-occurrence: explicit (a tagged <xref>) or inferred (prose / range).",
    "evidence": "The source sentence, with the raw <xref> markup inline, windowed past the reference.",
}
EXT_FIELDS = {
    "ref_type":        "usc / cfr / eo / public_law / omb (statutes), form (SF/OF/DD), or url.",
    "target":          "Canonical id so repeats share one node: 'usc:41/1303', 'eo:11246', 'form:SF-33', or the URL.",
    "locator":         "The specific subsection cited ('(a)(4)'), kept separate from the document id.",
    "node_label":      "Human-readable form ('41 U.S.C. 1303', 'Standard Form 33').",
    "citation":        "The reference verbatim, as written in the text.",
    "href":            "A resolvable link when the source had one (gov form pages, uscode.house.gov, …); '' if none.",
    "division_levels": "The citation parsed into ordered parts (['41','1303','a','4'], ['SF','33']).",
    "confidence":      "explicit / inferred — same meaning as internal.",
    "status":          "Same verification status as internal. (verified.json only)",
    "mentions":        "Every occurrence — list of {kind, evidence}.",
}
CHANGE_FIELDS = {
    "text":        "The exact changed words (find this in `text` to redline).",
    "fac":         "The FAC that made the change (from the rev attribute).",
    "case_number": "FAR case number, from the inline [CaseNumber] marker.",
    "why":         "Plain-language reason, from the inline [Why] marker; may be '' (e.g. table-cell edits).",
}

def _val(v):
    if isinstance(v, str):
        return json.dumps(v if len(v) <= 72 else v[:69] + "…", ensure_ascii=False)
    if isinstance(v, list):
        return f"[{len(v)} item(s)]" if v else "[]"
    return json.dumps(v, ensure_ascii=False)

def annotate(rec, fields, indent="  "):
    for k, doc in fields.items():
        if k not in rec:
            continue
        line = f"{indent}{k:20} = {_val(rec[k])}"
        print(line)
        for w in textwrap.wrap(doc, 96):
            print(f"{indent}{'':20}   · {w}")

def load(path_arg):
    cands = [path_arg] if path_arg else [os.path.join(HERE, "out", "FAR_verified.json"),
                                         os.path.join(HERE, "out", "FAR_chunks.json")]
    for p in cands:
        if p and os.path.exists(p):
            return json.load(open(p, encoding="utf-8")), p
    import chunker as ck                                  # fall back: parse a demo set live (no LLM, no `status`)
    cfg = {"regulation": "FAR", "url_template": "https://www.acquisition.gov/far/{num}",
           "input_dir": os.path.join(HERE, "dita") if os.path.exists(os.path.join(HERE, "dita"))
                        else os.path.normpath(os.path.join(HERE, "..", "dita")),
           "ditamap": "", "bottom_depth": 2, "bottom_level": "subparagraph",
           "files": ["52.212-5", "52.247-64", "52.225-3", "52.101", "52.219-9"]}
    rows, _, _ = ck.run_chunker(cfg)
    return rows, "(parsed live from DITA — no `status`; run the pipeline for verified output)"

def pick(data, **kw):
    for r in data:
        if all(r.get(k) == v for k, v in kw.items()):
            return r
    return None

def main():
    ap = argparse.ArgumentParser(description="Field guide + demo for the FAR JSON dataset")
    ap.add_argument("--file", help="path to a verified.json / chunks.json (default: out/, else parse live)")
    data, src = load(ap.parse_args().file)
    print("=" * 92)
    print("FAR dataset — field guide & demo")
    print("The dataset is a flat JSON ARRAY of 'chunk' records. Each record is one piece of the FAR.")
    print(f"Source: {src}   ({len(data)} records)")
    print("=" * 92)

    base = pick(data, citation="FAR-52.212-5", alternate="") or data[0]
    print(f"\n### A FULL RECORD — {base['citation']} (base clause) — every field annotated:\n")
    annotate(base, CHUNK_FIELDS)

    xr = next((c for r in data for c in r.get("cross_references", []) if c.get("mentions")), None)
    if xr:
        print("\n### cross_references[] — one entry:\n")
        annotate(xr, XREF_FIELDS)
        print("\n  …and one of its mentions[]:\n")
        annotate(xr["mentions"][0], MENTION_FIELDS, indent="    ")
    ext = next((c for r in data for c in r.get("external_references", [])), None)
    if ext:
        print("\n### external_references[] — one entry:\n")
        annotate(ext, EXT_FIELDS)
    chg = next((c for r in data for c in r.get("changes", [])), None)
    if chg:
        print("\n### changes[] — one entry (the change track):\n")
        annotate(chg, CHANGE_FIELDS)

    print("\n### SPECIAL RECORDS — the same fields, different values:\n")
    alt = pick(data, citation="FAR-52.247-64", alternate="1") or pick(data, citation="FAR-52.212-5", alternate="1")
    if alt:
        print(f"  • Alternate chunk:        {alt['citation']}  alternate={alt['alternate']!r}  "
              f"instrument={alt['instrument']!r}  date={alt['date']!r}  prescribed_by={alt['prescribed_by']!r}")
    refalt = next((c for r in data if r.get("citation") == "FAR-52.212-5"
                   for c in r.get("cross_references", []) if c.get("alternate")), None)
    if refalt:
        print(f"  • Reference TO an alternate: target={refalt['target']!r}  alternate={refalt['alternate']!r}  "
              f"(resolves to the chunk with that citation+alternate)")
    img = pick(data, citation="FAR-52.101", alternate="")
    if img and img.get("images"):
        print(f"  • Record with an image:   {img['citation']}  images={img['images']}  "
              f"('[IMAGE: …]' marks its spot in `text`)")
    rsv = next((r for r in data if r.get("reserved")), None)
    if rsv:
        print(f"  • Reserved placeholder:   {rsv['citation']}  alternate={rsv['alternate']!r}  reserved=True  "
              f"text={rsv['text'][:40]!r}")
    print("\nFull field-by-field reference: pipeline/VERIFIED_FORMAT.md")

if __name__ == "__main__":
    main()
