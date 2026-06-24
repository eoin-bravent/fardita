#!/usr/bin/env python3
"""Extract FAR DITA into a flat JSON list of chunks (units + top-level paragraphs)
with FAR-decomposed identifiers and evidence-bearing cross_references.

Usage:
  python extract_json.py --subset             # curated 8  -> test_data/far_test_subset.json
  python extract_json.py --all                # whole FAR   -> test_data/far_full.json
  python extract_json.py 5.301 7.105 [-o OUT] # specific sections -> OUT (default far_selection.json)

v1 scope: only files named like a FAR citation (^\\d+\\.\\d+(-\\d+)?$) are processed;
parts, subparts, covers, matrices, etc. are skipped and reported. Tables and images
are dropped from the grid but left as obvious inline placeholders in the text.
"""
import os, re, sys, json, shutil
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_to_sql as P                       # load_concept, norm, tok, DITA, all_files

SUBSET = ["5.201", "5.202", "5.203", "5.205", "5.207", "12.603", "5.101", "6.302-2"]
NUMERIC = re.compile(r"^\d+\.\d+(-\d+)?$")
WB, WA = 90, 75                                 # context window: chars before link / after ref end
MIN_TEXT = 40                                   # skip near-empty units
ASSETS = os.path.join(P.DITA, "test_data", "assets")

# ---------- citation helpers ----------
def components(section_num, paragraph=""):
    part = section_num.split(".")[0]
    rest = section_num.split(".", 1)[1] if "." in section_num else ""
    pre, subsec = ((rest.split("-", 1) + [""])[:2]) if "-" in rest else (rest, "")
    return {"part": part, "subpart": pre[:-2], "section": pre[-2:],
            "subsection": subsec, "paragraph": paragraph, "subparagraph": ""}

def href_to_citation(href):
    frag = href.split("#")[-1]
    if frag.startswith("FAR_Subpart_"):
        return "subpart " + frag[len("FAR_Subpart_"):].replace("_", ".")
    if frag.startswith("FAR_Part_"):
        return "part " + frag[len("FAR_Part_"):]
    if frag.startswith("FAR_"):
        t = frag[4:].split("_")
        if len(t) >= 2:
            return f"{t[0]}.{t[1]}" + ("-" + "-".join(t[2:]) if len(t) > 2 else "")
    return frag

# ---------- table / image placeholders ----------
def table_marker(t, url):
    title = t.find(".//title")
    cap = P.norm("".join(title.itertext())) if title is not None else ""
    return f'[TABLE OMITTED{f": \"{cap}\"" if cap else ""} — see {url}]'

def image_marker(im, url):
    href = im.get("href") or ""
    alt_el = im.find("alt")
    alt = P.norm("".join(alt_el.itertext())) if alt_el is not None else ""
    ref = href
    src = os.path.join(P.DITA, href) if href else ""
    if src and os.path.isfile(src):                       # copy binary if present (Graphics/ is empty now)
        os.makedirs(ASSETS, exist_ok=True)
        try:
            shutil.copyfile(src, os.path.join(ASSETS, os.path.basename(href)))
            ref = "assets/" + os.path.basename(href)
        except OSError:
            pass
    return f'[FIGURE OMITTED{f": \"{alt}\"" if alt else ""} — {ref} — see {url}]'

# ---------- text flattening ----------
def flatten_p(p, url):
    label, parts = "", []
    if p.text:
        parts.append(p.text)
    for ch in p:
        if ch.tag == "ph" and ch.get("props") == "autonumber":
            label = P.norm(ch.text)
        elif ch.tag == "image":
            parts.append(" " + image_marker(ch, url) + " ")
        else:
            parts.append("".join(ch.itertext()))
        if ch.tail:
            parts.append(ch.tail)
    body = P.norm("".join(parts))
    return f"{label} {body}".strip() if label else body

def flatten_block(el, url):
    """Flatten a block container (conbody or li) child-by-child, in document order."""
    out = []
    for ch in el:
        if ch.tag == "p":
            t = flatten_p(ch, url)
            if t:
                out.append(t)
        elif ch.tag == "ol":
            for li in ch.findall("./li"):
                t = flatten_li(li, url)
                if t:
                    out.append(t)
        elif ch.tag in ("table", "simpletable"):
            out.append(table_marker(ch, url))
        elif ch.tag in ("image", "fig"):
            img = ch if ch.tag == "image" else ch.find(".//image")
            if img is not None:
                out.append(image_marker(img, url))
    return out

def flatten_li(li, url):
    return " ".join(flatten_block(li, url))

def flatten_section(conbody, url):
    return "\n".join(flatten_block(conbody, url))

# ---------- evidence context ----------
def render_scope(ps):
    """Plain text of the whole <p> sequence (autonumber labels kept, paragraphs joined by space)
    plus the [start, end) span of every <xref> anchor. Windowing over this lets a context cross
    paragraph boundaries, so a reference at the end of a paragraph still shows what follows."""
    parts, xpos = [], []
    L = lambda: sum(len(z) for z in parts)
    def ser(node):
        if node.text:
            parts.append(node.text)
        for ch in list(node):
            if ch.tag == "xref":
                s = L(); parts.append("".join(ch.itertext())); xpos.append((ch, s, L()))
            elif ch.tag == "ph" and ch.get("props") == "autonumber":
                parts.append((ch.text or "") + " ")
            else:
                ser(ch)
            if ch.tail:
                parts.append(ch.tail)
    for p in ps:
        ser(p); parts.append(" ")
    return "".join(parts), xpos

def _window(text, a, b):
    return ("…" if a > 0 else "") + P.norm(text[a:b]) + ("…" if b < len(text) else "")

# ---------- cross references ----------
def _group_refs(refs):
    """Group per-occurrence refs by single `target` -> {target, confidence, mentions:[{kind,context}]}.
    confidence = 'explicit' if any mention is an explicit <xref> link, else 'inferred'."""
    out, index = [], {}
    for r in refs:
        t = r["target"]
        if t not in index:
            index[t] = len(out)
            out.append({"target": t, "confidence": r["kind"], "mentions": []})
        out[index[t]]["mentions"].append({"kind": r["kind"], "context": r["context"]})
        if r["kind"] == "explicit":
            out[index[t]]["confidence"] = "explicit"
    return out

def collect_refs(ps, sec_num, url):
    text, xpos = render_scope(ps)
    refs = []
    for el, s, e in xpos:
        href = el.get("href") or ""
        if el.get("scope") == "external" or href.startswith("http"):
            continue
        base = href_to_citation(href)
        q = re.match(r"\s*((?:\([A-Za-z0-9]+\))+)", text[e:e + 40])        # qualifier right after the anchor
        endref = e + (q.end() if q else 0)
        a, b = max(0, s - WB), min(len(text), endref + WA)
        lit = f'<xref href="{href}">{text[s:e]}</xref>'                    # splice the raw markup back in
        ctx = ("…" if a > 0 else "") + P.norm(text[a:s] + lit + text[e:b]) + ("…" if b < len(text) else "")
        if q:
            refs.append({"kind": "inferred", "target": base + q.group(1), "context": ctx})
        else:
            refs.append({"kind": "explicit", "target": base, "context": ctx})
    for m in re.finditer(r"\(([a-z])\)\s+through\s+\(([a-z])\)", text):     # range -> one literal-span ref
        refs.append({"kind": "inferred", "target": f"{sec_num}({m.group(1)})-({m.group(2)})",
                     "context": _window(text, max(0, m.start() - WB), min(len(text), m.end() + WA))})
    for m in re.finditer(r"paragraphs?\s+(\([a-z0-9]+\)(?:\([a-z0-9]+\))*)\s+of this section", text):
        refs.append({"kind": "inferred", "target": sec_num + m.group(1),
                     "context": _window(text, max(0, m.start() - WB), min(len(text), m.end() + WA))})
    return _group_refs(refs)

# ---------- build rows ----------
def make_row(citation, typ, comp, url, ps, sec_num, text):
    return {"citation": citation, "type": typ,
            "part": comp["part"], "subpart": comp["subpart"], "section": comp["section"],
            "subsection": comp["subsection"], "paragraph": comp["paragraph"],
            "subparagraph": comp["subparagraph"], "url": url,
            "cross_references": collect_refs(ps, sec_num, url), "text": text}

def build(far):
    c = P.load_concept(far)
    if c is None:
        return []
    title_el = c.find("./title")
    num_ph = title_el.find(".//ph[@props='autonumber']") if title_el is not None else None
    sec_num = P.norm(num_ph.text) if num_ph is not None else far
    conbody = c.find(".//conbody")
    if conbody is None:
        return []
    url = f"https://www.acquisition.gov/far/{sec_num}"
    unit_text = flatten_section(conbody, url)
    paras = []
    for ol in conbody.findall("./ol"):
        for li in ol.findall("./li"):
            p0 = li.find("./p")
            ph = p0.find("./ph[@props='autonumber']") if p0 is not None else None
            label = P.tok(P.norm(ph.text)) if (ph is not None and P.norm(ph.text)) else ""
            if not label:
                continue
            paras.append(make_row(f"{sec_num}({label})", "paragraph", components(sec_num, label),
                                  url, list(li.iter("p")), sec_num, flatten_li(li, url)))
    if len(unit_text) < MIN_TEXT and not paras:
        return []
    typ = "subsection" if "-" in sec_num else "section"
    unit = make_row(sec_num, typ, components(sec_num), url, list(conbody.iter("p")), sec_num, unit_text)
    return [unit] + paras

# ---------- sort (FAR citation order) ----------
def sort_key(r):
    g = lambda v: int(v) if v.isdigit() else 0
    par = r["paragraph"]
    prank = (ord(par.lower()) - 96) if (len(par) == 1 and par.isalpha()) else (int(par) if par.isdigit() else 0)
    return (g(r["part"]), g(r["subpart"]), g(r["section"]), g(r["subsection"]), prank)

# ---------- run ----------
def process(names):
    rows, skipped, failed = [], [], []
    for far in names:
        try:
            r = build(far)
            (rows.extend(r) if r else skipped.append(far))
        except Exception as e:                  # one bad file can't kill the run
            failed.append((far, repr(e)[:90]))
    rows.sort(key=sort_key)
    return rows, skipped, failed

def main():
    args = sys.argv[1:]
    out = None
    if "-o" in args:
        i = args.index("-o"); out = args[i + 1]; del args[i:i + 2]
    if not args:
        print(__doc__); return
    if args[0] == "--all":
        names = [n for n in P.all_files() if NUMERIC.match(n)]
        out = out or os.path.join(P.DITA, "test_data", "far_full.json")
    elif args[0] == "--subset":
        names, out = SUBSET, out or os.path.join(P.DITA, "test_data", "far_test_subset.json")
    else:
        names = args
        out = out or os.path.join(P.DITA, "test_data", "far_selection.json")

    rows, skipped, failed = process(names)
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(rows, open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=True)

    print(f"wrote {out}  ({os.path.getsize(out)/1e6:.1f} MB)")
    print(f"  files: {len(names)}  ->  processed {len(names)-len(skipped)-len(failed)}"
          f"  skipped {len(skipped)}  failed {len(failed)}")
    print(f"  rows: {len(rows)}   by type: {dict(Counter(r['type'] for r in rows))}")
    print(f"  cross_refs by kind: "
          f"{dict(Counter(k for r in rows for k in (cr['kind'] for cr in r['cross_references'])))}")
    if failed[:5]:
        print("  sample failures:", failed[:5])

if __name__ == "__main__":
    main()
