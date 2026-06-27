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
import os, re, sys, json, glob
import xml.etree.ElementTree as ET
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DITA = os.path.join(HERE, "dita")               # .dita source files live here

SUBSET = ["5.201", "5.202", "5.203", "5.205", "5.207", "12.603", "5.101", "6.302-2"]
NUMERIC = re.compile(r"^\d+\.\d+(-\d+)?$")
WB, WA = 90, 75                                 # context window: chars before link / after ref end
MIN_TEXT = 40                                   # skip near-empty units

# ---------- DITA loading + whitespace helpers ----------
def norm(s):
    """Collapse DITA whitespace to single spaces."""
    return re.sub(r"\s+", " ", (s or "")).strip()

def tok(label):
    """'(a)' -> 'a', '(1)' -> '1', '(iii)' -> 'iii'."""
    return re.sub(r"[^A-Za-z0-9]", "", label or "")

def load_concept(far):
    path = os.path.join(DITA, far + ".dita")
    if not os.path.exists(path):
        sys.stderr.write(f"  SKIP (missing): {far}.dita\n")
        return None
    raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
    return ET.fromstring(raw).find(".//concept")

def all_files():
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(DITA, "*.dita")))

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

# ---------- inline rendering: tables -> HTML, images -> [IMAGE: id] ----------
def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def img_id(href):
    """Stable id for an image = its filename (unique across the FAR; keys the downstream image store)."""
    return os.path.basename(href) or (href or "image")

def image_token(im):
    """Inline placeholder carrying the image id; the binary + description live in a downstream store."""
    return f"[IMAGE: {img_id(im.get('href') or '')}]"

def table_to_html(t):
    """Render a CALS <table> as clean minimal HTML (caption + colspan), inlined in the chunk text."""
    title = t.find("./title")
    cap = _esc(norm("".join(title.itertext()))) if title is not None else ""
    tg = t.find(".//tgroup")
    if tg is None:
        return f"<table>{f'<caption>{cap}</caption>' if cap else ''}</table>"
    colnum = {}
    for cs in tg.findall("./colspec"):
        nm, cn = cs.get("colname"), cs.get("colnum")
        if nm:
            colnum[nm] = int(cn) if (cn and cn.isdigit()) else len(colnum) + 1
    def span(e):
        a, b = e.get("namest"), e.get("nameend")
        return colnum[b] - colnum[a] + 1 if (a in colnum and b in colnum and colnum[b] >= colnum[a]) else 1
    def section(sec, tag):
        out = []
        for row in sec.findall("./row"):
            cells = []
            for e in row.findall("./entry"):
                sp = span(e)
                attr = f' colspan="{sp}"' if sp > 1 else ""
                cells.append(f"<{tag}{attr}>{_esc(norm(''.join(e.itertext())))}</{tag}>")
            out.append("<tr>" + "".join(cells) + "</tr>")
        return "".join(out)
    parts = ["<table>"]
    if cap:
        parts.append(f"<caption>{cap}</caption>")
    thead, tbody = tg.find("./thead"), tg.find("./tbody")
    if thead is not None:
        parts.append("<thead>" + section(thead, "th") + "</thead>")
    if tbody is not None:
        parts.append("<tbody>" + section(tbody, "td") + "</tbody>")
    parts.append("</table>")
    return "".join(parts)

def collect_images(el):
    """Deduped list of image ids in this chunk, e.g. ["piid.png"]. Each appears inline in `text` as an
    [IMAGE: id] token; the binary + plain-language description live in a downstream store keyed by id.
    (Tables aren't listed — their content is inlined as HTML directly in `text`.)"""
    images = []
    for ch in el.iter():
        if ch.tag == "image":
            iid = img_id(ch.get("href") or "")
            if iid not in images:
                images.append(iid)
    return images

# ---------- text flattening ----------
def flatten_p(p, url):
    label, parts = "", []
    if p.text:
        parts.append(p.text)
    for ch in p:
        if ch.tag == "ph" and ch.get("props") == "autonumber":
            label = norm(ch.text)
        elif ch.tag == "image":
            parts.append(" " + image_token(ch) + " ")
        else:
            parts.append("".join(ch.itertext()))
        if ch.tail:
            parts.append(ch.tail)
    body = norm("".join(parts))
    return f"{label} {body}".strip() if label else body

def flatten_nodes(nodes, url, skip_ids=None):
    """Flatten an explicit sequence of block nodes child-by-child, in document order.
    skip_ids: optional set of id(element) to omit — e.g. the end-of-clause marker <p> or an
    alternate <section> that's extracted separately and must not pollute the basic-clause text."""
    out = []
    for ch in nodes:
        if skip_ids and id(ch) in skip_ids:
            continue
        if ch.tag == "p":
            t = flatten_p(ch, url)
            if t:
                out.append(t)
        elif ch.tag in ("ol", "ul"):                 # ul = bulleted list (e.g. definition lists); flatten like ol
            for li in ch.findall("./li"):
                t = flatten_li(li, url)
                if t:
                    out.append(t)
        elif ch.tag in ("table", "simpletable"):
            out.append(table_to_html(ch))
        elif ch.tag in ("image", "fig"):
            img = ch if ch.tag == "image" else ch.find(".//image")
            if img is not None:
                out.append(image_token(img))
    return out

def flatten_block(el, url, skip_ids=None):
    """Flatten a block container (conbody or li) child-by-child, in document order."""
    return flatten_nodes(list(el), url, skip_ids)

def flatten_li(li, url):
    return " ".join(flatten_block(li, url))

def flatten_section(conbody, url, skip_ids=None):
    return "\n".join(flatten_block(conbody, url, skip_ids))

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
    return ("…" if a > 0 else "") + norm(text[a:b]) + ("…" if b < len(text) else "")

# ---------- range expansion (no spans: every range -> its explicit members) ----------
_ROMAN_VAL = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_ROMAN_SEQ = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
              (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
_SEP = r"(?:through|thru|to|–|—|-)"               # range separators: words, en/em dash, hyphen
_PAREN = r"(?:\([A-Za-z0-9]+\))"
# (a)-(c) / (a) to (c) / 7.101(a) - 7.101(c) / (a)(1) through (a)(6)
RANGE_PAREN = re.compile(r"(?P<lb>\d+\.\d+(?:-\d+)?)?\s*(?P<lt>" + _PAREN + r"+)\s*" + _SEP +
                         r"\s*(?P<rb>\d+\.\d+(?:-\d+)?)?\s*(?P<rt>" + _PAREN + r"+)", re.I)
# 6.302-1 through 6.302-5 / 52.219-3 to 52.219-5 / 6.302-1 through -5
RANGE_DASH = re.compile(r"(?P<base>\d+\.\d+)-(?P<s>\d+)\s*(?:through|thru|to|–|—|\s-\s)\s*"
                        r"(?:(?P=base)-)?(?P<e>\d+)(?!\.\d)", re.I)

def _roman_to_int(s):
    s = s.lower()
    if not s or any(c not in _ROMAN_VAL for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        v = _ROMAN_VAL[c]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total

def _int_to_roman(n):
    out = ""
    for v, sym in _ROMAN_SEQ:
        while n >= v:
            out += sym; n -= v
    return out

def _enumerate_tokens(start, end):
    """'a'..'f' / '1'..'6' / 'ii'..'vi' -> explicit member tokens (case preserved).
    Returns None when the axis is unsafe/ambiguous (decision 1a) so the range is skipped."""
    if not start or not end:
        return None
    if start.isdigit() and end.isdigit():                           # numeric
        a, b = int(start), int(end)
        return [str(n) for n in range(a, b + 1)] if 0 <= b - a <= 40 else None
    if (start.upper() == start) != (end.upper() == end):            # mixed case -> not one axis
        return None
    upper, lo, hi = start.upper() == start, start.lower(), end.lower()
    is_roman = lambda t: all(c in _ROMAN_VAL for c in t)
    if is_roman(lo) and is_roman(hi) and (len(lo) > 1 or len(hi) > 1):   # roman (one multi-char endpoint)
        a, b = _roman_to_int(lo), _roman_to_int(hi)
        if a is None or b is None or not 0 < b - a <= 40:
            return None
        toks = [_int_to_roman(n) for n in range(a, b + 1)]
        return [t.upper() for t in toks] if upper else toks
    if len(lo) == 1 and len(hi) == 1 and lo.isalpha() and hi.isalpha():  # single letters
        if is_roman(lo) and is_roman(hi):
            return None                                             # (i)-(v), (v)-(x): letter vs roman — skip
        a, b = ord(lo), ord(hi)
        if not 0 <= b - a <= 25:
            return None
        toks = [chr(n) for n in range(a, b + 1)]
        return [t.upper() for t in toks] if upper else toks
    return None

def _range_refs(text, sec_num):
    """Find every range in `text` and emit one inferred ref per member (no spans).
    All members of a range share the range's surrounding context."""
    out = []
    for m in RANGE_PAREN.finditer(text):
        lt = re.findall(r"\(([A-Za-z0-9]+)\)", m.group("lt"))
        rt = re.findall(r"\(([A-Za-z0-9]+)\)", m.group("rt"))
        fixed = lt[:-1]
        if rt[:-1] and rt[:-1] != fixed:             # cross-prefix range, e.g. (a)(1)-(b)(3) — unsafe
            continue
        members = _enumerate_tokens(lt[-1], rt[-1])
        if not members:
            continue
        base = m.group("lb") or m.group("rb")
        if not base:                                  # no base glued to the range -> carry the nearest
            left = re.findall(r"\d+\.\d+(?:-\d+)?", text[max(0, m.start() - 120):m.start()])
            base = left[-1] if left else sec_num      # most recent citation in the list, else this section
        prefix = base + "".join(f"({t})" for t in fixed)
        ctx = _window(text, max(0, m.start() - WB), min(len(text), m.end() + WA))
        for tok in members:
            out.append({"kind": "inferred", "target": f"{prefix}({tok})", "evidence": ctx})
    for m in RANGE_DASH.finditer(text):
        a, b = int(m.group("s")), int(m.group("e"))
        if not 0 < b - a <= 40:
            continue
        ctx = _window(text, max(0, m.start() - WB), min(len(text), m.end() + WA))
        for n in range(a, b + 1):
            out.append({"kind": "inferred", "target": f"{m.group('base')}-{n}", "evidence": ctx})
    return out

# ---------- cross references ----------
def _group_refs(refs):
    """Group per-occurrence refs by (target, alternate) -> {target, alternate, confidence, mentions}.
    Keyed by (target, alternate) so a reference to a clause and to its Alternate stay DISTINCT edges
    (e.g. '52.247-64' vs '52.247-64' alternate '1'). `alternate` is '' for the base clause.
    confidence = 'explicit' if any mention is an explicit <xref> link, else 'inferred'."""
    out, index = [], {}
    for r in refs:
        key = (r["target"], r.get("alternate", ""))
        if key not in index:
            index[key] = len(out)
            out.append({"target": r["target"], "alternate": r.get("alternate", ""),
                        "confidence": r["kind"], "mentions": []})
        out[index[key]]["mentions"].append({"kind": r["kind"], "evidence": r["evidence"]})
        if r["kind"] == "explicit":
            out[index[key]]["confidence"] = "explicit"
    return out

# "…Alternate I (date) of <CLAUSE>"  and  "<CLAUSE> …, with Alternate I" — a reference to a clause
# variant. Detected per anchor and carried as a separate `alternate` field (arabic), distinct from
# the base-clause edge; '' when the reference is to the base clause.
ALT_OF   = re.compile(r"\bAlternate\s+([IVXLCDM]+)\s*(?:\([^)]*\)\s*)?of\s*$", re.I)
ALT_WITH = re.compile(r"\bwith\s+Alternate\s+([IVXLCDM]+)\b", re.I)
_ROMAN_ALT = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
def _alt_arabic(roman):
    """'I'->'1', 'IV'->'4'; '' for empty/non-roman."""
    s, total, prev = (roman or "").lower(), 0, 0
    for ch in reversed(s):
        v = _ROMAN_ALT.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return str(total) if total else ""

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
        ctx = ("…" if a > 0 else "") + norm(text[a:s] + lit + text[e:b]) + ("…" if b < len(text) else "")
        alt = ""                                                          # clause-Alternate qualifier, if adjacent
        m = ALT_OF.search(text[max(0, s - 80):s])                         # "Alternate I … of" right before the anchor
        if m:
            alt = m.group(1).upper()
        else:
            after = text[endref:endref + 160]                             # "…, with Alternate I" within the same item
            cut = after.find(". ")                                        # don't borrow the next clause's alternate
            m = ALT_WITH.search(after if cut < 0 else after[:cut])
            if m:
                alt = m.group(1).upper()
        target = base + (q.group(1) if q else "")
        refs.append({"kind": "inferred" if q else "explicit", "target": target,
                     "alternate": _alt_arabic(alt), "evidence": ctx})
    refs.extend(_range_refs(text, sec_num))                                # ranges -> explicit members
    for m in re.finditer(r"paragraphs?\s+(\([a-z0-9]+\)(?:\([a-z0-9]+\))*)\s+of this section", text):
        refs.append({"kind": "inferred", "target": sec_num + m.group(1),
                     "evidence": _window(text, max(0, m.start() - WB), min(len(text), m.end() + WA))})
    return _group_refs(refs)

# ---------- external references (other government documents: USC / CFR / EO / Pub. L. / OMB) ----------
# Each becomes a node = the doc/section (canonical `target`); finer subdivisions ride on the edge as
# `locator`. `division_levels` is the full parse (title, section, subsections…), mirroring the DITA
# decomposition. The parser catches these rigid formats; the LLM catches the long tail.
def _parens_list(p):
    return re.findall(r"\(([A-Za-z0-9]+)\)", p or "")

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")

def _b_usc_ch(m):
    t, n = m.group("title"), m.group("num")
    return {"ref_type": "usc", "target": f"usc:{t}/ch{n}", "node_label": f"{t} U.S.C. chapter {n}",
            "division_levels": [t, "chapter", n], "locator": ""}

def _b_usc(m):
    t, s, p = m.group("title"), m.group("sec"), m.group("parens") or ""
    return {"ref_type": "usc", "target": f"usc:{t}/{s}", "node_label": f"{t} U.S.C. {s}",
            "division_levels": [t, s] + _parens_list(p), "locator": p}

def _b_cfr(m):
    t, part, s, p = m.group("title"), m.group("part"), m.group("sec"), m.group("parens") or ""
    node = f"cfr:{t}/{part}" + (f".{s}" if s else "")
    return {"ref_type": "cfr", "target": node, "node_label": f"{t} CFR {part}" + (f".{s}" if s else ""),
            "division_levels": [t, part] + ([s] if s else []) + _parens_list(p), "locator": p}

def _b_eo(m):
    n = m.group("num")
    return {"ref_type": "eo", "target": f"eo:{n}", "node_label": f"E.O. {n}", "division_levels": [n], "locator": ""}

def _b_publ(m):
    c, n = m.group("cong"), m.group("num")
    return {"ref_type": "public_law", "target": f"publ:{c}-{n}", "node_label": f"Pub. L. {c}-{n}",
            "division_levels": [c, n], "locator": ""}

def _b_omb(m):
    s, n = m.group("ser"), m.group("num")
    return {"ref_type": "omb", "target": f"omb:{s}-{n}", "node_label": f"OMB Circular {s}-{n}",
            "division_levels": [s, n], "locator": ""}

# Only rigid, well-formed citation types. Named statutes ("the X Act") were too noisy to parse
# reliably (line-wrapped names fragment into many nodes; capitalized phrases over-match) — dropped.
EXTERNAL_PATTERNS = [
    (re.compile(r"(?P<title>\d+)\s+U\.S\.C\.\s+chapter\s+(?P<num>\d+)", re.I), _b_usc_ch),
    (re.compile(r"(?P<title>\d+)\s+U\.S\.C\.\s*(?P<sec>\d+[A-Za-z]?)(?P<parens>(?:\([A-Za-z0-9]+\))*)"), _b_usc),
    (re.compile(r"(?P<title>\d+)\s+CFR\s+(?:part\s+)?(?P<part>\d+)(?:\.(?P<sec>\d+))?(?P<parens>(?:\([A-Za-z0-9]+\))*)", re.I), _b_cfr),
    (re.compile(r"(?:E\.O\.|Executive\s+Order)\s+(?P<num>\d+)", re.I), _b_eo),
    (re.compile(r"(?:Pub\.\s*L\.|Public\s+Law)\s+(?:No\.?\s*)?(?P<cong>\d+)-(?P<num>\d+)", re.I), _b_publ),
    (re.compile(r"OMB\s+Circular\s+(?:No\.?\s*)?(?P<ser>[A-Z])-(?P<num>\d+)", re.I), _b_omb),
]
EXTERNAL_TYPES = {"usc", "cfr", "eo", "public_law", "omb"}

def parse_external(s):
    """Normalize one external citation string -> {ref_type, target, node_label, division_levels, locator, citation}.
    Returns None unless it's one of the rigid types (USC/CFR/EO/Pub.L./OMB)."""
    s = norm(s)                                            # collapse any line-wrap whitespace
    for rx, build in EXTERNAL_PATTERNS:
        m = rx.search(s)
        if m:
            r = build(m)
            r["citation"] = s
            return r
    return None

FORM_RX = re.compile(r"\b(Standard Form|SF|Optional Form|OF|DD Form|DD)\s*-?\s*(\d+[A-Za-z]?)\b", re.I)

def parse_form(anchor):
    """'Standard Form 33' / 'SF 33' / 'DD Form 254' -> {ref_type:'form', target:'form:SF-33', …}. Else None."""
    m = FORM_RX.search(anchor or "")
    if not m:
        return None
    w = m.group(1).lower()
    series = "SF" if w.startswith(("standard", "sf")) else "OF" if w.startswith(("optional", "of")) else "DD"
    return {"ref_type": "form", "target": f"form:{series}-{m.group(2)}", "node_label": (anchor or "").strip(),
            "division_levels": [series, m.group(2)], "locator": "", "citation": (anchor or "").strip()}

def build_external_edge(document, section, fallback_type="other"):
    """Build a canonical external edge from a human-edited (document, section). Re-detects the type
    from the text; falls back to the row's ref_type (e.g. 'act') for named documents the regex can't type."""
    document, section = (document or "").strip(), (section or "").strip()
    for src in (f"{document}{section}", f"{document} {section}".strip(), document):
        p = parse_external(src)
        if p:
            if section:
                p["locator"] = section
            return p
    rt = fallback_type or "other"
    parts = re.findall(r"\d+|[A-Za-z]+", section)
    label = document
    cite = (f"section {section} of the {document}" if section else document) if rt == "act" else (document + section)
    return {"ref_type": rt, "target": f"{rt}:{_slug(document)}", "node_label": label,
            "division_levels": [document] + parts, "locator": section, "citation": cite}

def collect_external_refs(ps):
    """External (non-FAR) references in a unit, grouped by (target, locator):
      - statutory citations in the prose (USC/CFR/EO/Pub.L./OMB), via regex;
      - tagged external links (<xref scope=external / http href>): forms (form:SF-33) and other URLs,
        plus the resolvable `href` attached to whichever ref the link points to.
    Each entry: {ref_type, target, locator, node_label, division_levels, citation, href, confidence, mentions}."""
    text, xpos = render_scope(ps)
    out, index = [], {}
    def add(r, ev, kind, href=""):
        key = (r["target"], r.get("locator", ""))
        if key not in index:
            index[key] = len(out)
            out.append({"ref_type": r["ref_type"], "target": r["target"], "locator": r.get("locator", ""),
                        "node_label": r.get("node_label", r["target"]),
                        "division_levels": r.get("division_levels", []), "citation": r.get("citation", ""),
                        "href": href, "confidence": "explicit", "mentions": []})
        e = out[index[key]]
        if href and not e["href"]:
            e["href"] = href                               # enrich with the resolvable link when present
        if ev and not any(m["evidence"] == ev for m in e["mentions"]):
            e["mentions"].append({"kind": kind, "evidence": ev})
        return e
    # (A) statutory citations written in the prose
    for rx, build in EXTERNAL_PATTERNS:
        for m in rx.finditer(text):
            r = build(m); r["citation"] = norm(m.group(0))
            add(r, _window(text, max(0, m.start() - WB), min(len(text), m.end() + WA)), "inferred")
    # (B) tagged external links — forms, other URLs, and hrefs for statutory links
    for el, s, e in xpos:
        href = el.get("href") or ""
        if not (el.get("scope") == "external" or href.startswith("http")) or href.startswith("mailto"):
            continue
        anchor = norm(text[s:e])
        ev = _window(text, max(0, s - WB), min(len(text), e + WA))
        r = parse_external(anchor) or parse_form(anchor)
        if r:
            r.setdefault("citation", anchor)
            add(r, ev, "explicit", href)
        elif href.startswith("http"):                      # a plain external URL (NIST, agency page, …)
            tgt = href.rstrip("/")
            add({"ref_type": "url", "target": tgt, "node_label": anchor or tgt,
                 "division_levels": [], "locator": "", "citation": anchor or tgt}, ev, "explicit", href)
    return out

# ---------- build rows ----------
def make_row(citation, typ, comp, url, ps, sec_num, text, el):
    return {"citation": citation, "type": typ,
            "part": comp["part"], "subpart": comp["subpart"], "section": comp["section"],
            "subsection": comp["subsection"], "paragraph": comp["paragraph"],
            "subparagraph": comp["subparagraph"], "url": url,
            "cross_references": collect_refs(ps, sec_num, url),
            "external_references": collect_external_refs(ps),
            "images": collect_images(el), "text": text}

def build(far):
    c = load_concept(far)
    if c is None:
        return []
    title_el = c.find("./title")
    num_ph = title_el.find(".//ph[@props='autonumber']") if title_el is not None else None
    sec_num = norm(num_ph.text) if num_ph is not None else far
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
            label = tok(norm(ph.text)) if (ph is not None and norm(ph.text)) else ""
            if not label:
                continue
            paras.append(make_row(f"{sec_num}({label})", "paragraph", components(sec_num, label),
                                  url, list(li.iter("p")), sec_num, flatten_li(li, url), li))
    if len(unit_text) < MIN_TEXT and not paras:
        return []
    typ = "subsection" if "-" in sec_num else "section"
    unit = make_row(sec_num, typ, components(sec_num), url, list(conbody.iter("p")), sec_num, unit_text, conbody)
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
        names = [n for n in all_files() if NUMERIC.match(n)]
        out = out or os.path.join(DITA, "test_data", "far_full.json")
    elif args[0] == "--subset":
        names, out = SUBSET, out or os.path.join(DITA, "test_data", "far_test_subset.json")
    else:
        names = args
        out = out or os.path.join(DITA, "test_data", "far_selection.json")

    rows, skipped, failed = process(names)
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(rows, open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=True)

    print(f"wrote {out}  ({os.path.getsize(out)/1e6:.1f} MB)")
    print(f"  files: {len(names)}  ->  processed {len(names)-len(skipped)-len(failed)}"
          f"  skipped {len(skipped)}  failed {len(failed)}")
    print(f"  rows: {len(rows)}   by type: {dict(Counter(r['type'] for r in rows))}")
    print(f"  cross_refs by confidence: "
          f"{dict(Counter(cr['confidence'] for r in rows for cr in r['cross_references']))}")
    if failed[:5]:
        print("  sample failures:", failed[:5])

if __name__ == "__main__":
    main()
