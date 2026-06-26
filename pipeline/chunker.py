#!/usr/bin/env python3
"""Configurable DITA chunker (stage 1 of the pipeline).

Reuses the proven helpers in ../extract_json.py but generalizes:
  * chunk at every level from the file's own unit (section/subsection) down to a
    configurable bottom level (paragraph .. subunit-depth-N);
  * carry a `regulation` field (FAR / DFARS / …);
  * scan a configurable input folder and emit a manifest of processed + skipped files.
"""
import os, re, sys, json
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract_json as X       # reuse norm/tok, flatten_*, collect_refs, components, href_to_citation

# Level ladder.  Indices 0..3 come from the citation number; 4+ from the (a)(1)(i)… chain.
PARA_LEVELS = ["paragraph", "subparagraph",
               "subunit-depth-1", "subunit-depth-2", "subunit-depth-3", "subunit-depth-4"]
LEVEL_DEPTH = {"section": 0, "subsection": 0,
               **{name: i + 1 for i, name in enumerate(PARA_LEVELS)}}
NUMERIC = re.compile(r"^\d+\.\d+(-\d+)?$")
MIN_TEXT = 40

def level_name(depth):
    return PARA_LEVELS[depth - 1] if depth >= 1 else "section"

def load_concept_path(path):
    try:
        raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
        return ET.fromstring(raw).find(".//concept")
    except (ET.ParseError, OSError):
        return None

def decompose(sec_num, tokens, field_levels):
    base = X.components(sec_num)                # part / subpart / section / subsection (bare)
    d = {k: base[k] for k in ("part", "subpart", "section", "subsection")}
    for i, name in enumerate(field_levels):    # paragraph .. bottom
        d[name] = tokens[i] if i < len(tokens) else ""
    return d

def build(path, far, cfg):
    """Return (rows, skip_reason). rows is None when skipped."""
    c = load_concept_path(path)
    if c is None:
        return None, "no <concept> / parse error"
    title = c.find("./title")
    num_ph = title.find(".//ph[@props='autonumber']") if title is not None else None
    sec_num = X.norm(num_ph.text) if num_ph is not None else far
    conbody = c.find(".//conbody")
    if conbody is None:
        return None, "no <conbody>"
    url = cfg["url_template"].format(num=sec_num)
    reg = cfg["regulation"]
    bottom = cfg["bottom_depth"]
    field_levels = PARA_LEVELS[:bottom]        # decomposition fields run part..bottom

    def row(number, typ, tokens, ps, text, el):
        r = {"citation": f"{reg}-{number}", "regulation": reg, "type": typ}
        r.update(decompose(sec_num, tokens, field_levels))
        r["url"] = url
        r["cross_references"] = X.collect_refs(ps, sec_num, url)
        r["external_references"] = X.collect_external_refs(ps)
        r["images"] = X.collect_images(el)                     # tables are inlined as HTML in `text`
        r["text"] = text
        return r

    unit_text = X.flatten_section(conbody, url)
    rows = [row(sec_num, "subsection" if "-" in sec_num else "section", [],
                list(conbody.iter("p")), unit_text, conbody)]

    def walk(ol, toks):
        for li in ol.findall("./li"):
            p0 = li.find("./p")
            ph = p0.find("./ph[@props='autonumber']") if p0 is not None else None
            label = X.tok(X.norm(ph.text)) if (ph is not None and X.norm(ph.text)) else ""
            if not label:
                for sub in li.findall("./ol"):
                    walk(sub, toks)
                continue
            nt = toks + [label]
            d = len(nt)
            if d <= bottom:
                cit = sec_num + "".join(f"({t})" for t in nt)
                rows.append(row(cit, level_name(d), nt, list(li.iter("p")), X.flatten_li(li, url), li))
            if d < bottom:
                for sub in li.findall("./ol"):
                    walk(sub, nt)

    if bottom >= 1:
        for ol in conbody.findall("./ol"):
            walk(ol, [])

    if len(rows) == 1 and len(unit_text) < MIN_TEXT:
        return None, "near-empty"
    return rows, None

def sort_key(r):
    g = lambda v: int(v) if v.isdigit() else 0
    def rank(t):
        return (ord(t.lower()) - 96) if (len(t) == 1 and t.isalpha()) else (int(t) if t.isdigit() else 0)
    para = [r.get(n, "") for n in PARA_LEVELS]
    return (g(r["part"]), g(r["subpart"]), g(r["section"]), g(r["subsection"]),
            tuple(rank(t) for t in para))

def _resolve_files(cfg):
    """Return (paths, explicit, missing). If cfg['files'] is set, use exactly those;
    otherwise scan input_dir for *.dita."""
    files = cfg.get("files")
    if not files:
        paths = sorted(os.path.join(cfg["input_dir"], f)
                       for f in os.listdir(cfg["input_dir"]) if f.endswith(".dita"))
        return paths, False, []
    paths, missing = [], []
    for f in files:
        cands = [f, f + ".dita", os.path.join(cfg["input_dir"], f),
                 os.path.join(cfg["input_dir"], f + ".dita")]
        hit = next((c for c in cands if os.path.isfile(c)), None)
        (paths.append(hit) if hit else missing.append(f))
    return paths, True, missing

def run_chunker(cfg):
    """Resolve the file set (shared by the LLM stage), chunk, return (rows, manifest, sources)."""
    paths, explicit, missing = _resolve_files(cfg)
    rows, processed, skipped = [], [], []
    sources = {}                               # {unit_citation: .dita file path} — for the raw-file LLM pass
    for f in missing:
        skipped.append({"file": f, "reason": "not found"})
    for path in paths:
        far = os.path.splitext(os.path.basename(path))[0]
        if not explicit and not NUMERIC.match(far):   # name filter only applies when scanning a folder
            skipped.append({"file": far, "reason": "non-numeric (part/subpart/cover/matrix/…)"})
            continue
        try:
            r, reason = build(path, far, cfg)
        except Exception as e:                 # one bad file can't kill the run
            skipped.append({"file": far, "reason": f"error: {repr(e)[:80]}"})
            continue
        if r:
            rows.extend(r)
            processed.append(far)
            sources[r[0]["citation"]] = path   # r[0] is the unit (section/subsection) row
        else:
            skipped.append({"file": far, "reason": reason})
    rows.sort(key=sort_key)
    manifest = {"regulation": cfg["regulation"], "input_dir": os.path.abspath(cfg["input_dir"]),
                "bottom_level": cfg["bottom_level"], "files_seen": len(paths),
                "processed_count": len(processed), "skipped_count": len(skipped),
                "processed": processed, "skipped": skipped}
    return rows, manifest, sources
