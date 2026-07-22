#!/usr/bin/env python3
"""acquisition.gov FAR-archive adapters for the versioned store.

Converts one downloaded archive edition (a folder from https://www.acquisition.gov/archives)
into standard chunk rows and merges them via Store.merge_snapshot() -- extending the store's
history earlier than the GitHub DITA repo (which starts at FAC 2023-02).

Eras (detected per folder):
  ditaot   2021-07 .. 2025-06   DITA-OT XHTML, one FAR_Part_N.html per part (rendered from
                                the same FrameMaker/DITA lineage as the GitHub repo; markup
                                preserves outputclass, autonumber spans, Endofclause and
                                Alternate sections)
  (older eras -- FAC 2005-xx FrameMaker HTML, FAC 1997/1990 plain HTML -- get their own
   parsers later; this module is structured per-era.)

THE seam rule: the flattening here must reproduce pipeline/chunker.py + extract_json.py
byte-for-byte over store.HASH_FIELDS, or a backfill would fabricate thousands of spurious
"changed" rows at the boundary edition.  Run `seam` against an edition present in both
sources before any real ingest.

Why hints exist: the FM HTML encodes VISUAL depth (ListLn classes) while the chunker's
line/row structure follows the DITA's STRUCTURAL nesting, and the FM->DITA conversion was
irregular (ol-inside-p definitions, Runin items, unlabeled li's, li lists promoted or
demoted a level).  Those distinctions are unrecoverable from the HTML alone, so
`derive-hints` extracts each unit's line/row skeleton once from the store itself; units
absent from the hints (sections that died before the store's history begins) fall back to
class-based rules.

Usage:
  python archive_adapter.py derive-hints --store-dir S --date 2023-03-16
  python archive_adapter.py meta   <edition_dir>                      # detected FAC + date
  python archive_adapter.py chunk  <edition_dir> -o rows.json         # adapter output
  python archive_adapter.py seam   rows.json --store-dir S --date D   # HASH_FIELDS diff
  python archive_adapter.py ingest rows.json --store-dir S --date D \
         --source-version "FAC 2023-02 March 16, 2023" --commit 2023-02_HTML_Files
"""
import os
import re
import sys
import json
import glob
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))          # chunker/parsers
_PKG = os.path.dirname(HERE)                               # chunker/
DATA_DIR = os.path.join(_PKG, "data")    # checked-in inputs: agencies.json, per-reg dates files
CACHE_DIR = os.path.join(_PKG, "cache")  # derived + regenerable (gitignored): era surveys, hints
os.makedirs(CACHE_DIR, exist_ok=True)
from chunker import extract_json as X     # norm/tok/components, _esc, _window, _range_refs, _group_refs, …
from chunker import chunker as CK         # ALT_OPENER/ALT_DATE/PRESCRIBED, roman_to_arabic, sort_key, NUMERIC
from chunker.store import Store, HASH_FIELDS, content_hash

try:
    import lxml.html
except ImportError:
    sys.exit("archive_adapter requires lxml (pip install lxml)")

SOURCE = "acquisition-gov-archive"
# Adapter citation grammar: like the chunker's NUMERIC but allowing ANY subsection
# dash depth -- AFARS' older editions have three-level sections ('5101.602-2-91')
# that were renumbered away by the modern DITA era. FAR never uses >1 level, so the
# extension is inert there.
NUMERIC = re.compile(r"^\d+\.\d+(-\d+)*$")
MIN_TEXT = CK.MIN_TEXT
LISTL = re.compile(r"\bListL(\d+)\b")
LINE_CAP, HEAD_CAP = 240, 48      # hint sizes: line text head / row head
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _pipeline_rev():
    p = subprocess.run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else "unknown"


# ---------------------------------------------------------------- shared helpers
def _cls(el):
    return " " + (el.get("class") or "") + " "


def _is_autonum(el):
    return el.tag == "span" and "autonumber" in _cls(el)


def _list_depth(el):
    m = LISTL.search(el.get("class") or "")
    return int(m.group(1)) if m else 0


def _img_token(img):
    return f"[IMAGE: {X.img_id(img.get('src') or '')}]"


def _text(el):
    return el.text_content() if el is not None else ""


def _label_tok(p):
    """Row-label token of a paragraph: first DIRECT-child autonumber span's .text,
    tokenized ('(a)' -> 'a').  '' when there is none, or when the span holds nested
    markup ('(<em>A</em>)' -> '(' -> '') -- exactly what the chunker's walk() sees."""
    raw = next((c.text or "" for c in p if _is_autonum(c)), "")
    return X.tok(X.norm(raw))


def _is_fillin(el):
    """FM fill-in artifacts ('<span>__</span>' checkbox blanks): rendered by the HTML
    toolchain but not present in the DITA text the chunker flattens."""
    return (el.tag == "span" and not (el.get("class") or "")
            and re.fullmatch(r"_+", X.norm(el.text_content() or "")) is not None)


# ---------------------------------------------------------------- ditaot era: flattening
def flatten_p(p, inline=False, nospace=()):
    """Two modes, mirroring the two ways the chunker flattens a paragraph:

    inline=False -- X.flatten_p on a DITA <li> paragraph: the DIRECT-child autonumber
      span becomes a leading label + ONE injected space ('(i) Contracting officers…'
      even when the source has no whitespace after the span), and only the span's
      .text survives (nested markup like '(<i>A</i>)' contributes just '(').
    inline=True  -- raw itertext, for content that was INSIDE a <p> in the DITA
      (ol-inside-p definitions): no label injection ('(1)Is an established…'), and
      the autonumber's full text survives ('(A)').

    `nospace`: store-derived label joints written WITHOUT a following space in the
    DITA ('(ii)Sold in su…') -- suppresses the injected space for exactly those.

    Both modes: images -> ' [IMAGE: id] '; 'fm:Text' anchors (DITA-OT auto-filling an
    EMPTY dita xref with the target's heading) and fill-in '<span>__</span>' blanks
    contribute nothing."""
    label, parts = "", []
    if p.text:
        parts.append(p.text)
    for ch in p:
        if not isinstance(ch.tag, str):          # comments / PIs
            if ch.tail:
                parts.append(ch.tail)
            continue
        if _is_autonum(ch):
            if inline:
                parts.append(_text(ch))
            else:
                label = X.norm(ch.text or "")
        elif ch.tag == "img":
            parts.append(" " + _img_token(ch) + " ")
        elif ch.tag == "a" and "fm:Text" in _cls(ch):
            pass                                 # empty xref in the DITA source
        elif _is_fillin(ch):
            pass
        elif ch.tag == "br":
            parts.append(" ")
        else:
            parts.append(_text(ch))
        if ch.tail:
            parts.append(ch.tail)
    body = X.norm("".join(parts))
    if not label:
        return body
    if body and nospace:
        probe = f"{label}{body}"
        if any(probe[:len(k)] == k for k in nospace):
            return f"{label}{body}"
    return f"{label} {body}".strip()


def table_html(t):
    """Mirror X.table_to_html's minimal serialization from the DITA-OT <table> rendering:
    <table><caption>…</caption><thead><tr><th colspan=…>…  cells norm'd + escaped.
    The caption joins its title/desc children with a space, as the chunker does."""
    cap_el = t.find("caption")
    cap = ""
    if cap_el is not None:
        pieces = [cap_el.text or ""]
        for c in cap_el:
            pieces.extend([_text(c), c.tail or ""])
        cap = X._esc(X.norm(" ".join(p for p in pieces if p and p.strip())))
    def sec(container, tag):
        out = []
        for row in container.findall(".//tr"):
            cells = []
            for c in row:
                if c.tag not in ("td", "th"):
                    continue
                sp = c.get("colspan") or "1"
                sp = int(sp) if sp.isdigit() else 1
                attr = f' colspan="{sp}"' if sp > 1 else ""
                cells.append(f"<{tag}{attr}>{X._esc(X.norm(_text(c)))}</{tag}>")
            out.append("<tr>" + "".join(cells) + "</tr>")
        return "".join(out)
    parts = ["<table>"]
    if cap:
        parts.append(f"<caption>{cap}</caption>")
    thead, tbody = t.find("thead"), t.find("tbody")
    if thead is not None:
        parts.append("<thead>" + sec(thead, "th") + "</thead>")
    if tbody is not None:
        parts.append("<tbody>" + sec(tbody, "td") + "</tbody>")
    elif thead is None and t.find(".//tr") is not None:      # rows straight under <table>
        parts.append("<tbody>" + sec(t, "td") + "</tbody>")
    parts.append("</table>")
    return "".join(parts)


def flatten_block(ch, inline=False, nospace=()):
    """One block -> list of flattened items (mirror X.flatten_nodes dispatch)."""
    if ch.tag == "p":
        t = flatten_p(ch, inline, nospace)
        return [t] if t else []
    if ch.tag == "table":
        return [table_html(ch)]
    if ch.tag in ("ul", "ol"):
        out = []
        for li in ch.findall("li"):
            t = flatten_li(li)
            if t:
                out.append(t)
        return out
    if ch.tag in ("figure", "fig"):
        img = ch.find(".//img")
        return [_img_token(img)] if img is not None else []
    if ch.tag == "img":
        return [_img_token(ch)]
    return []                                    # sections / notes / divs are not chunk text


def flatten_li(li):
    out = []
    if li.text and X.norm(li.text):
        out.append(X.norm(li.text))
    for ch in li:
        out.extend(flatten_block(ch))
        if ch.tail and X.norm(ch.tail):
            out.append(X.norm(ch.tail))
    return " ".join(out)


# ---------------------------------------------------------------- line/row reconstruction
BLOCK_TAGS = ("p", "table", "figure", "img", "ul", "ol")


def _prefix_match(line_t, block_t):
    """Do a store line and a block flattening agree on their common head?"""
    n = min(len(line_t), len(block_t), LINE_CAP)
    return n > 0 and line_t[:n] == block_t[:n]


def _line_ns(lines, j):
    return tuple(lines[j].get("ns", ())) if j is not None and j < len(lines) else ()


def group_blocks(blocks, hints=None):
    """Reconstruct the chunker's LINE structure from the flat FM block sequence.

    Returns [{line_idx, ns, blocks}] -- one entry per line of the unit text; attached
    blocks join their group's line with spaces (the chunker's flatten_li behavior).
    line_idx indexes into hints['lines'] when the line was matched (rows are then
    extracted per line by extract_rows), else None; ns is that line's nospace keys.

    For units covered by hints, line breaks are decided by the store's own ordered
    line texts: a block whose flattened head agrees with the next store line starts
    a new line; a block agreeing with a LATER line (>=24 chars of evidence) jumps
    ahead, which keeps OLDER editions aligned across lines inserted or removed
    since; anything else attaches to the current line."""
    h = hints or {}
    lines = h.get("lines", [])
    known = bool(lines)
    groups, open_g, ptr = [], None, 0

    def new_group(ch, idx):
        g = {"line_idx": idx, "ns": _line_ns(lines, idx), "blocks": [ch]}
        groups.append(g)
        return g

    def try_line(ch, j):
        items = flatten_block(ch, nospace=_line_ns(lines, j))
        return items and _prefix_match(lines[j]["t"], items[0])

    def next_line(ch, t_plain):
        nonlocal ptr
        if ptr < len(lines) and try_line(ch, ptr):
            ptr += 1
            return ptr - 1
        if len(t_plain) >= 24:
            for j in range(ptr + 1, len(lines)):
                if try_line(ch, j):
                    ptr = j + 1
                    return j
        return None

    for ch in blocks:
        if not isinstance(ch.tag, str) or ch.tag not in BLOCK_TAGS:
            continue                        # section/div/note: skipped by the chunker too
        items = flatten_block(ch)
        if not items:
            continue                        # FM's empty separator <p>s: not in the DITA
        if known:
            j = next_line(ch, items[0])
            if j is not None:
                open_g = new_group(ch, j)
            elif open_g is not None:
                open_g["blocks"].append(ch)
            else:
                open_g = new_group(ch, None)
        else:                               # fallback: class-based rules
            d = _list_depth(ch) if ch.tag == "p" else None
            ltok = _label_tok(ch) if ch.tag == "p" else ""
            if d == 0 or (d is not None and ltok and d == 1) or open_g is None:
                open_g = new_group(ch, None)
            else:
                open_g["blocks"].append(ch)
    return groups


def _inline_group(g):
    """A line that begins as a PLAIN unlabeled paragraph carries ol-inside-p content:
    its attachments flatten itertext-style (no label injection)."""
    first = g["blocks"][0]
    return first.tag == "p" and _list_depth(first) == 0 and not _label_tok(first)


def group_text(g):
    inline = _inline_group(g)
    return " ".join(s for ch in g["blocks"]
                    for s in flatten_block(ch, inline, g.get("ns", ())))


def extract_rows(g, hints):
    """Paragraph-row segments inside one line group: [{label, blocks}].

    A chunker row is a top-level DITA <li>; a line may hold several (4.601's
    '(1) … (2) …' definitions share one line) or none.  The store's per-line row
    list (ordered {l: label, h: head}) drives the split: a block whose flattening
    agrees with the next expected row head (or, failing that, whose label token
    equals the expected label -- older editions where the text changed) starts a
    row; it runs to the next row start or the line's end."""
    if g["line_idx"] is None or _inline_group(g):
        return []
    rows_h = hints["lines"][g["line_idx"]].get("rows", [])
    if not rows_h:
        return []
    flats = []
    for ch in g["blocks"]:
        items = flatten_block(ch, nospace=g.get("ns", ()))
        flats.append(items[0] if items else "")
    segments, frm = [], 0
    for exp in rows_h:
        # head match first (authoritative); the label-token fallback (older editions
        # where the text changed) only if no later block carries the expected head --
        # otherwise a nested same-label item shadows the real row (52.209-5's roman
        # (ii) inside (D) vs the top-level letter (ii))
        found = None
        for i in range(frm, len(g["blocks"])):
            t = flats[i]
            n = min(len(exp["h"]), len(t))
            if n and exp["h"][:n] == t[:n]:
                found = i
                break
        if found is None:
            found = next((i for i in range(frm, len(g["blocks"]))
                          if g["blocks"][i].tag == "p"
                          and _label_tok(g["blocks"][i]) == exp["l"]), None)
        if found is not None:
            segments.append({"label": exp["l"], "start": found})
            frm = found + 1
    for k, seg in enumerate(segments):
        end = segments[k + 1]["start"] if k + 1 < len(segments) else len(g["blocks"])
        seg["blocks"] = g["blocks"][seg["start"]:end]
    return segments


# ---------------------------------------------------------------- ditaot era: references
def render_scope(ps):
    """Mirror X.render_scope over XHTML: plain text of the <p> sequence with the [start,end)
    span of every <a> anchor; autonumber spans keep their trailing space."""
    parts, xpos = [], []
    L = lambda: sum(len(z) for z in parts)
    def ser(node):
        if node.text:
            parts.append(node.text)
        for ch in node:
            if not isinstance(ch.tag, str):
                if ch.tail:
                    parts.append(ch.tail)
                continue
            if ch.tag == "a" and "fm:Text" not in _cls(ch):
                s = L(); parts.append(_text(ch)); xpos.append((ch, s, L()))
            elif _is_autonum(ch):
                parts.append((ch.text or "") + " ")
            elif _is_fillin(ch):
                pass
            else:
                ser(ch)
            if ch.tail:
                parts.append(ch.tail)
    for p in ps:
        ser(p); parts.append(" ")
    return "".join(parts), xpos


def collect_refs(ps, sec_num):
    """Mirror X.collect_refs from XHTML anchors (href '#FAR_5_207' / 'FAR_Part_5.html#FAR_5_207')."""
    text, xpos = render_scope(ps)
    refs = []
    for el, s, e in xpos:
        href = el.get("href") or ""
        if href.startswith(("http", "mailto")):
            continue
        frag = href.split("#")[-1]
        if not frag:
            continue
        base = X.href_to_citation("#" + frag)
        q = re.match(r"\s*((?:\([A-Za-z0-9]+\))+)", text[e:e + 40])
        endref = e + (q.end() if q else 0)
        a, b = max(0, s - X.WB), min(len(text), endref + X.WA)
        lit = f'<xref href="{href}">{text[s:e]}</xref>'
        ctx = ("…" if a > 0 else "") + X.norm(text[a:s] + lit + text[e:b]) + ("…" if b < len(text) else "")
        alt = ""
        before = X.norm(text[max(0, s - 250):s])
        m = X.ALT_OF.search(before)
        if m:
            alt = m.group(1).upper()
        else:
            after = X.norm(text[endref:endref + 250])
            cut = after.find(". ")
            m = X.ALT_WITH.search(after if cut < 0 else after[:cut])
            if m:
                alt = m.group(1).upper()
        target = base + (q.group(1) if q else "")
        refs.append({"kind": "inferred" if q else "explicit", "target": target,
                     "alternate": X._alt_arabic(alt), "evidence": ctx})
    refs.extend(X._range_refs(text, sec_num))
    for m in re.finditer(r"paragraphs?\s+(\([a-z0-9]+\)(?:\([a-z0-9]+\))*)\s+of this section", text):
        refs.append({"kind": "inferred", "target": sec_num + m.group(1),
                     "evidence": X._window(text, max(0, m.start() - X.WB),
                                           min(len(text), m.end() + X.WA))})
    return X._group_refs(refs)


def collect_external_refs(ps):
    """Mirror X.collect_external_refs from XHTML."""
    text, xpos = render_scope(ps)
    out, index = [], {}
    def add(r, ev, kind, href=""):
        key = (r["target"], r.get("locator", ""))
        if key not in index:
            index[key] = len(out)
            out.append({"ref_type": r["ref_type"], "target": r["target"],
                        "locator": r.get("locator", ""),
                        "node_label": r.get("node_label", r["target"]),
                        "division_levels": r.get("division_levels", []),
                        "citation": r.get("citation", ""),
                        "href": href, "confidence": "explicit", "mentions": []})
        e = out[index[key]]
        if href and not e["href"]:
            e["href"] = href
        if ev and not any(m["evidence"] == ev for m in e["mentions"]):
            e["mentions"].append({"kind": kind, "evidence": ev})
    for rx, build in X.EXTERNAL_PATTERNS:
        for m in rx.finditer(text):
            r = build(m); r["citation"] = X.norm(m.group(0))
            add(r, X._window(text, max(0, m.start() - X.WB), min(len(text), m.end() + X.WA)),
                "inferred")
    for el, s, e in xpos:
        href = el.get("href") or ""
        if not href.startswith("http"):
            continue
        anchor = X.norm(text[s:e])
        ev = X._window(text, max(0, s - X.WB), min(len(text), e + X.WA))
        r = X.parse_external(anchor) or X.parse_form(anchor)
        if r:
            r.setdefault("citation", anchor)
            add(r, ev, "explicit", href)
        else:
            tgt = href.rstrip("/")
            add({"ref_type": "url", "target": tgt, "node_label": anchor or tgt,
                 "division_levels": [], "locator": "", "citation": anchor or tgt},
                ev, "explicit", href)
    return out


# ---------------------------------------------------------------- ditaot era: units
def _title_of(article):
    """(autonumber, title-without-number-and-trailing-period) from the article's heading."""
    h = next((c for c in article if isinstance(c.tag, str)
              and re.match(r"h[1-6]$", c.tag or "") and "title" in _cls(c)), None)
    if h is None:
        return "", ""
    num = next((X.norm(_text(c)) for c in h.iter("span") if _is_autonum(c)), "")
    parts = [h.text or ""]
    for ch in h:
        if not _is_autonum(ch):
            parts.append(_text(ch))
        parts.append(ch.tail or "")
    t = X.norm("".join(parts))
    return num, (t[:-1].rstrip() if t.endswith(".") else t)


def _own_body(article):
    return next((c for c in article if isinstance(c.tag, str) and c.tag == "div"
                 and "body" in _cls(c)), None)


# A clause terminator is a MARKER-ONLY line ('(End of clause)') or a class-tagged
# paragraph -- NEVER prose that merely mentions the phrase. The old detectors used a
# bare substring test ('end of clause' in text), so any paragraph containing the words
# -- e.g. DFARS 227.7009-2's '(a) ... an example ... at the end of clause 252.227-7001'
# -- was mistaken for the terminator and DROPPED whole (silent legal-text loss). This
# helper requires the paragraph to REDUCE TO the marker after stripping parens,
# asterisks (elision rows), dashes, periods and whitespace.
_END_CORE_RX = re.compile(r"[\s()*.—–‒‐-]+")


def _end_marker(text, cls=""):
    """'(End of clause)' / '(End of provision)' / '' for a paragraph, by class first
    then marker-only text. Prose mentioning the phrase returns ''."""
    if "Endofclause" in cls:
        return "(End of clause)"
    if "Endofprovision" in cls:
        return "(End of provision)"
    core = _END_CORE_RX.sub(" ", X.norm(text)).strip().lower()
    if core == "end of clause":
        return "(End of clause)"
    if core == "end of provision":
        return "(End of provision)"
    return ""


def _find_end_and_alt(children):
    """Mirror CK.find_end_and_alt over the XHTML rendering."""
    end_text, end_el, end_idx = "", None, -1
    for i, ch in enumerate(children):
        if ch.tag != "p":
            continue
        em = _end_marker(_text(ch), _cls(ch))
        if em:
            end_text, end_el, end_idx = em, ch, i
            break
    alt = None
    for ch in (children[end_idx + 1:] if end_idx >= 0 else children):
        if ch.tag == "section" and ("Alternate" in _cls(ch)
                                    or any(CK.ALT_OPENER.match(flatten_p(p))
                                           for p in ch.findall("p"))):
            alt = ch
            break
    return end_text, end_el, alt


def _alt_spans(section):
    """Mirror CK.alt_spans: split the Alternate <section> into one span per alternate."""
    spans, cur = [], None
    if section is None:
        return spans
    for ch in section:
        if not isinstance(ch.tag, str) or ch.tag not in BLOCK_TAGS:
            continue
        roman = None
        if ch.tag == "p":
            m = CK.ALT_OPENER.match(flatten_p(ch))
            roman = m.group(1).upper() if m else None
        if roman:
            cur = {"roman": roman, "nodes": [ch]}
            spans.append(cur)
        elif cur is not None:
            cur["nodes"].append(ch)
    return spans


def _alt_meta(nodes):
    opener = flatten_p(nodes[0]) if nodes[0].tag == "p" else X.norm(_text(nodes[0]))
    lead = opener.split(".", 1)[0]
    dm, pm = CK.ALT_DATE.search(lead), CK.PRESCRIBED.search(opener)
    date = X.norm(f"{dm.group(1)} {dm.group(2)}") if dm else ""
    return date, (pm.group(1).replace(" ", "") if pm else "")


def _base_meta(children):
    """Mirror CK.base_meta over the first few TOP-LEVEL <p> blocks (in the DITA, li
    paragraphs are not conbody children -- their FM rendering is ListLn, so exclude it)."""
    date, prescribed_by = "", ""
    for p in [c for c in children if c.tag == "p" and _list_depth(c) == 0][:4]:
        t = X.norm(_text(p))
        if not prescribed_by:
            m = CK.PRESCRIBED.search(t)
            if m:
                prescribed_by = m.group(1).replace(" ", "")
        if not date and "SmCaps" in _cls(p):
            dm = CK.ALT_DATE.search(t)
            if dm:
                date = X.norm(f"{dm.group(1)} {dm.group(2)}")
    return date, prescribed_by


def _instrument_of(children, end_marker):
    if end_marker == "(End of clause)":
        return "clause"
    if end_marker == "(End of provision)":
        return "provision"
    for p in [c for c in children if c.tag == "p" and _list_depth(c) == 0][:3]:
        t = X.norm(_text(p)).lower()
        if "following clause" in t:
            return "clause"
        if "following provision" in t:
            return "provision"
    return ""


def _scan_images(elements):
    imgs = []
    for el in elements:
        for im in ([el] if el.tag == "img" else el.findall(".//img")):
            iid = X.img_id(im.get("src") or "")
            if iid not in imgs:
                imgs.append(iid)
    return imgs


def _ps_of(blocks):
    """All <p> descendants of a block list (for reference scope), document order."""
    out = []
    for b in blocks:
        if b.tag == "p":
            out.append(b)
        out.extend(b.findall(".//p"))
    return out


def build_unit(article, sec_num, sec_title, cfg, hints=None):
    """Mirror CK.build for one section/subsection article. Returns rows (possibly [])."""
    body = _own_body(article)
    if body is None:
        return []
    children = [c for c in body if isinstance(c.tag, str)]
    url = cfg["url_template"].format(num=sec_num)
    reg = cfg["regulation"]

    base_type = "subsection" if "-" in sec_num else "section"
    unit_end, end_el, alt_section = _find_end_and_alt(children)
    if hints is not None and "end" in hints:
        # the store's marker is canon: '' means the DITA marker sits INSIDE the body
        # text (52.228-14) -- keep the paragraph in the text stream in that case
        if hints["end"] == "" and end_el is not None:
            end_el = None
        unit_end = hints["end"]
    instrument = _instrument_of(children, unit_end)
    bdate, bpresc = _base_meta(children) if instrument else ("", "")

    def row(number, typ, tokens, ps, text, scan, alternate="",
            date="", prescribed_by="", end_marker=""):
        r = {"citation": f"{reg}-{number}", "regulation": reg,
             "source_version": cfg.get("source_version", ""),
             "pipeline_version": cfg.get("pipeline_version", ""),
             "type": typ, "instrument": instrument, "alternate": alternate,
             "part_title": "", "subpart_title": "",
             "section_title": "", "subsection_title": ""}
        r.update(CK.decompose(sec_num, tokens, ["paragraph"][:cfg["bottom_depth"]]))
        r["url"] = url
        r["cross_references"] = collect_refs(ps, sec_num)
        r["external_references"] = collect_external_refs(ps)
        r["images"] = _scan_images(scan)
        r["changes"] = []                       # archives carry no rev markup (hash-excluded)
        r["date"] = date
        r["prescribed_by"] = prescribed_by
        r["reserved"] = X.norm(text).rstrip(".").lower().endswith("[reserved]")
        r["end_marker"] = end_marker
        r["text"] = text
        return r

    alt_set = {id(alt_section)} if alt_section is not None else set()
    base_blocks = [c for c in children
                   if id(c) not in alt_set and (end_el is None or id(c) != id(end_el))]
    groups = group_blocks(base_blocks, hints)
    unit_text = "\n".join(t for t in (group_text(g) for g in groups) if t)
    base_ps = _ps_of(base_blocks)
    rows = [row(sec_num, base_type, [], base_ps, unit_text, base_blocks,
                date=bdate, prescribed_by=bpresc, end_marker=unit_end)]

    for sp in _alt_spans(alt_section):
        nodes = sp["nodes"]
        aid = CK.roman_to_arabic(sp["roman"])
        adate, apresc = _alt_meta(nodes)
        ah = (hints or {}).get("alts", {}).get(aid)
        agroups = group_blocks(nodes, ah)
        atext = "\n".join(t for t in (group_text(g) for g in agroups) if t)
        rows.append(row(sec_num, base_type, [], _ps_of(nodes), atext, nodes,
                        alternate=aid, date=adate, prescribed_by=apresc))

    if cfg["bottom_depth"] >= 1 and hints:
        for g in groups:
            for seg in extract_rows(g, hints):
                cit = f"{sec_num}({seg['label']})"
                inline = _inline_group(g)
                text = " ".join(s for ch in seg["blocks"]
                                for s in flatten_block(ch, inline, g.get("ns", ())))
                rows.append(row(cit, CK.level_name(1), [seg["label"]],
                                _ps_of(seg["blocks"]), text, seg["blocks"]))
    elif cfg["bottom_depth"] >= 1:
        for g in groups:                        # fallback for unknown units
            first = g["blocks"][0]
            ltok = _label_tok(first) if first.tag == "p" else ""
            if ltok and _list_depth(first) == 1:
                text = group_text(g)
                rows.append(row(f"{sec_num}({ltok})", CK.level_name(1), [ltok],
                                _ps_of(g["blocks"]), text, g["blocks"]))

    own = "subsection_title" if "-" in sec_num else "section_title"
    for r in rows:
        r[own] = sec_title
    if len(rows) == 1 and len(unit_text) < cfg.get("min_text", MIN_TEXT):
        return []
    return rows


# ---------------------------------------------------------------- ditaot era: edition walk
def chunk_edition_ditaot(edition_dir, cfg, hints=None):
    """All chunk rows for one DITA-OT-era archive edition folder."""
    hints = hints or {}
    unit_hints = hints.get("units", {})
    rows, manifest = [], {"processed": [], "skipped": []}
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))

    def walk(article, part_no, subpart_no):
        num, title = _title_of(article)
        pm = re.match(r"^Part\s+(\d+)$", num or "")
        sm = re.match(r"^Subpart\s+([\d.]+)$", num or "")
        if pm or sm:
            title = re.sub(r"^[-–—]\s*", "", title)     # heading is 'Part 5 - Title'
        if pm:
            part_no = pm.group(1)
            part_titles.setdefault(part_no, title)      # store-derived titles win
        elif sm:
            subpart_no = sm.group(1)
            subpart_titles.setdefault(sm.group(1), title)
        elif num and NUMERIC.match(num):
            r = build_unit(article, num, title, cfg, unit_hints.get(num))
            if r:
                rows.extend(r)
                manifest["processed"].append(num)
            else:
                manifest["skipped"].append({"file": num, "reason": "near-empty / no body"})
        for ch in article:
            if isinstance(ch.tag, str) and ch.tag == "article":
                walk(ch, part_no, subpart_no)

    root = edition_root(edition_dir)
    files = sorted((f for f in glob.glob(os.path.join(root, "*.html"))
                    if re.match(r"(FAR|AFARS)[-_](Part|PART)[-_]\d+\.html$",
                                os.path.basename(f))),
                   key=lambda p: int(re.search(r"(\d+)", os.path.basename(p)).group(1)))
    if not files:
        raise SystemExit(f"no <REG>_Part_*.html in {edition_dir} -- not a ditaot-era folder?")
    for f in files:
        root = lxml.html.parse(f).getroot()
        for art in root.iter("article"):
            if art.getparent().tag != "article" or "nested0" in _cls(art):
                walk(art, None, None)
                break            # one top article per file

    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ---------------------------------------------------------------- ditaot-topics era
# The acquisition.gov "copypaste" per-TOPIC DITA-OT render (2020+): same markup as
# the ditaot era (nested <article>, autonumber spans, DC.Type/legacy-compat head) but
# shipped as one file per TOPIC ('GSAM-501.107.html') or per PART
# ('Part_2001_T48_606311.html'), agency-prefixed names the ditaot lister rejects.
# Reuses build_unit/_title_of wholesale; aggregate files (a chapter file containing
# the parts) are deduped first-come by section number.

_DTT_SKIP_RX = re.compile(r"(?i)blank|toc|index|search|cover|foreword|copyright|map"
                          r"|placeholder")
# per-PART aggregate render filenames: 'DFARS_PART_204.html', 'Part_1200_T48_505.html'
_DTT_PART_RX = re.compile(r"(?i)(^|_)part[_-]?\d+([_.]|$)")


_DTT_COMPANION_TITLE = re.compile(
    r"(?i)^\s*(ANNEX|EXHIBIT|ATTACHMENT|APPENDIX)\s+[A-Z0-9]")


def _dtt_is_companion(path):
    """True if a topic file is a COMPANION document (an annex/exhibit/attachment form
    template -- NMCARS ANNEX 1 J&A, Business Clearance Memorandum), not a numbered
    regulation section. These carry the citation in their h1 title, not the filename
    (NMCARS 'd1e5087.html' -> 'ANNEX 2 - BUSINESS CLEARANCE MEMORANDUM'), so a basename
    skip can't catch them. A different document class -- never section rows -- so drop
    from source, matching how the transit eras drop MP/IG/Annex files."""
    try:
        raw = open(path, encoding="utf-8", errors="replace").read(4000)
    except OSError:
        return False
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", raw)
    if not m:
        return False
    return bool(_DTT_COMPANION_TITLE.match(re.sub(r"<[^>]+>", " ", m.group(1)).strip()))


# ---- companion-document classification (Decision A: capture, don't drop) --------
def classify_companion(name, title="", citation=""):
    """COMPANION_DOCS §6 classifier: return a lowercase doc_class
    (mp|ig|annex|attachment|appendix|exhibit) for a companion unit, or None for regulation.
    Consolidates the old _dtt_is_companion title check and the farsite skip regex; checks the
    filename, the raw citation/autonumber, then the DITA title. PGI is NOT a companion here
    (it is its own first-class store, DFARSPGI)."""
    for probe in (os.path.basename(name or "").lower(), (citation or "").lower()):
        if re.search(r"(?:^|[_\- ])mp[\s._\-]?\d", probe):
            return "mp"
        if re.search(r"(?:^|[_\- ])ig[\s._\-]?\d", probe):
            return "ig"
        for cls in ("annex", "attachment", "appendix", "exhibit"):
            # not \b: an underscore ('Attachment_5601') is a word char, so \b never
            # matches there -- require the token to be followed by a separator/digit/end
            if re.search(rf"(?:^|[_\- ]){cls}(?=[\s._\-\d]|$)", probe):
                return cls
    t = re.sub(r"<[^>]+>", " ", title or "").strip()
    m = re.match(r"(?i)^\s*(annex|exhibit|attachment|appendix)\b", t)
    if m:
        return m.group(1).lower()
    m = re.match(r"(?i)^\s*(mandatory procedure|informational guidance)\b", t)
    if m:
        return "mp" if m.group(1)[0].lower() == "m" else "ig"
    return None


def companion_identity(agency, doc_class, citation="", title="", filename=""):
    """Class-prefixed companion citation '<AG>-<CLASS>-<localid>' + the localid. localid is
    the autonumber/citation with its class token stripped, else the title, else the filename
    stem (COMPANION_DOCS §5)."""
    raw = (citation or "").split("(")[0].strip()
    if raw.upper().startswith(agency.upper() + "-"):   # the chunker prepends '<REG>-'
        raw = raw[len(agency) + 1:]
    if not raw:
        t = re.sub(r"<[^>]+>", " ", title or "").strip()
        raw = t or os.path.splitext(os.path.basename(filename or ""))[0]
    localid = re.sub(r"(?i)^(mp|ig|annex|attachment|appendix|exhibit)[\s._\-]*", "", raw)
    localid = re.sub(r"\s+", "", localid).strip("._-") or raw
    return f"{agency}-{doc_class.upper()}-{localid}", localid


def _dtt_unit_key(basename):
    """Logical unit a topic filename addresses, stripped of the render-specific
    '_T48_<id>' suffix so the two parallel citation-style series a copypaste render
    ships collapse to one key: 'Part_1216_T48_5053714.html' and '..5053715.html' ->
    'part_1216'; 'Section_1201_105_1_T48_..' -> 'section_1201_105_1'; GSAM
    'GSAM-501.107.html' (no T48) -> its own name."""
    b = re.sub(r"(?i)\.html?$", "", basename)
    b = re.sub(r"(?i)_T48_\d+$", "", b)
    return b.lower()


def _dtt_dedup(files):
    """Keep ONE file per logical unit. The copypaste renders ship each part/section
    TWICE -- a CFR-expanded citation style ('(TAR) 48 CFR 1252.216-71') and the
    web-short style ('1252.216-71') that matches the DITA canon store -- under
    different _T48_ ids. Reading one and auditing leaves the other as bogus residue.
    Within a unit, prefer the render with the FEWEST literal '48 CFR' expansions
    (the canonical short form); ties break on sorted name."""
    groups = {}
    for f in files:
        groups.setdefault(_dtt_unit_key(os.path.basename(f)), []).append(f)
    out = []
    for k, fs in groups.items():
        if len(fs) == 1:
            out.append(fs[0])
            continue
        out.append(min(sorted(fs),
                       key=lambda f: open(f, encoding="utf-8", errors="replace")
                       .read().count("48 CFR")))
    return sorted(out)


def _dtt_files(edition_dir):
    """The ONE render to read for a ditaot-topics edition. These folders routinely
    ship the same content two or three times -- copypaste-AllTopic/FullParts/Subparts
    sibling trees (identical basenames), TWO citation-style series inside one such
    tree, a per-PART aggregate beside per-topic files, or a Word-export duplicate next
    to the DITA topics -- so a naive 'every .html' walk parses (harmless, deduped) but
    AUDITS the duplicates as unaccounted source. This picks a single complete,
    de-duplicated render; the audit is handed the same list.

    Precedence: GSAM html_part/ ; then one copypaste-* dir (FullParts > Subparts >
    AllTopic) ; then per-PART aggregate files if present ; else all DC.Type topics."""
    def content(fs):
        return _dtt_dedup([f for f in fs
                           if "__MACOSX" not in f
                           and not _DTT_SKIP_RX.search(os.path.basename(f))
                           and not _dtt_is_companion(f)])

    hp = content(glob.glob(os.path.join(edition_dir, "**", "html_part", "*.html"),
                           recursive=True))
    if hp:
        return hp
    for pref in ("copypaste-FullParts", "copypaste-Subparts", "copypaste-AllTopic"):
        dirs = glob.glob(os.path.join(edition_dir, "**", pref), recursive=True)
        if dirs:
            return content(glob.glob(os.path.join(dirs[0], "*.html")))

    named = re.compile(r"(?i)^([a-z]+[-_])?(\d{1,4}\.\d|(part|subpart|section|chapter)[-_])")
    all_topics, part_agg = [], []
    for f in sorted(glob.glob(os.path.join(edition_dir, "**", "*.html"),
                              recursive=True)):
        b = os.path.basename(f)
        if "__MACOSX" in f or _DTT_SKIP_RX.search(b):
            continue
        if not (named.match(b)
                or "legacy-compat" in open(f, encoding="utf-8", errors="replace").read(500)
                or "DC.Type" in open(f, encoding="utf-8", errors="replace").read(500)):
            continue
        if _dtt_is_companion(f):
            continue
        (part_agg if _DTT_PART_RX.search(b) else all_topics).append(f)
    # a per-part aggregate render is complete and duplicate-free; prefer it over the
    # loose per-topic files that ship alongside it
    return _dtt_dedup(part_agg) if part_agg else _dtt_dedup(all_topics)


def chunk_edition_ditaot_topics(edition_dir, cfg, hints=None):
    """All chunk rows for one per-topic DITA-OT render edition folder."""
    hints = hints or {}
    unit_hints = hints.get("units", {})
    rows, manifest = [], {"processed": [], "skipped": []}
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    seen = set()

    def walk(article, part_no, subpart_no):
        num, title = _title_of(article)
        num = re.sub(r"(?i)^PGI\s+", "", num or "")   # 'PGI 204.7103' autonumbers
        if not num and title:
            # some topic renders (NMCARS d1e*, DFARS PGI) put the citation INSIDE the
            # title text ('PART 5206 COMPETITION…', 'SUBPART 5206.2 - FULL…',
            # '5206.202 Establishing…', 'PGI 204.7103 Procedures') with no autonumber
            t = re.sub(r"(?i)^PGI\s+", "", title)
            m = re.match(r"(?i)^(PART|SUBPART)\s+([\d.]+)\s*[-–—:]?\s*(.*)$", t)
            x = WW_SECTION_NUM.match(t)
            if m:
                num = f"{m.group(1).title()} {m.group(2).rstrip('.')}"
                title = m.group(3).strip()
                if title.isupper():
                    title = title.title()
            elif x and NUMERIC.match(x.group(1)):
                num, title = x.group(1), x.group(2).strip().rstrip(".")
        pm = re.match(r"^Part\s+(\d+)$", num or "")
        sm = re.match(r"^Subpart\s+([\d.]+)$", num or "")
        if pm or sm:
            title = re.sub(r"^[-–—]\s*", "", title)
        if pm:
            part_no = pm.group(1)
            part_titles.setdefault(part_no, title)
        elif sm:
            subpart_no = sm.group(1)
            subpart_titles.setdefault(sm.group(1), title)
            # A subpart article can carry substantive OWN body text -- a scope note or
            # 'As used in this subpart --' definitions block (NMCARS 5206.3) that no
            # section owns. Conserve it as the subpart's scope section (FAR convention
            # <part>.<subpart>00, e.g. 5206.3 -> 5206.300); build_unit reads only this
            # article's own body, never the nested section articles.
            if "." in subpart_no and _own_body(article) is not None:
                p, s = subpart_no.split(".", 1)
                scope = f"{p}.{s}00"
                if scope not in seen and NUMERIC.match(scope):
                    r = build_unit(article, scope, title, cfg, unit_hints.get(scope))
                    if r:
                        seen.add(scope)
                        rows.extend(r)
                        manifest["processed"].append(scope)
        elif num and NUMERIC.match(num):
            if num in seen:                       # aggregate + topic file overlap
                manifest["skipped"].append({"file": num, "reason": "duplicate render"})
            else:
                r = build_unit(article, num, title, cfg, unit_hints.get(num))
                if r:
                    seen.add(num)                 # only CONSUME the number once a row is
                    rows.extend(r)                # actually produced -- a render often
                    manifest["processed"].append(num)  # ships an empty structural stub of
                else:                             # a section BEFORE its real content
                    manifest["skipped"].append({"file": num,  # article; consuming on the
                                                "reason": "near-empty / no body"})  # stub
                                                  # dropped the real one as a "duplicate"
        for ch in article:
            if isinstance(ch.tag, str) and ch.tag == "article":
                walk(ch, part_no, subpart_no)

    files = _dtt_files(edition_dir)
    for f in files:
        try:
            root = lxml.html.parse(f).getroot()
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f),
                                        "reason": repr(e)[:60]})
            continue
        for art in root.iter("article"):
            if art.getparent().tag != "article" or "nested0" in _cls(art):
                walk(art, None, None)
                break                             # one top article per file

    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ================================================================ WEBWORKS-2005 ERA
# FrameMaker->WebWorks XHTML (the "FAC 2005-xx" folders, ~2005-2018). One HTML file
# per SUBPART ('Subpart 5_1.html'); Part 52 clauses grouped ('52_207_211.html').
# FLAT sibling structure under <body>: <h3 class="pSection"> opens a section;
# <p class="pBody"> is a top-level paragraph, <p class="pIndentedN"> is depth N
# (STRUCTURAL here, unlike the ditaot ListLn visual depth), <p class="pBodyCtr*">
# are centered lines (a clause's dated title, the End-of-clause marker). Paragraph
# labels ((a)/(1)/(i)) are LITERAL text; <a name="wp…"> are position anchors (drop);
# cross-refs are <a href="Subpart 5_2.html#wp…">5.201</a> with the citation in the
# ANCHOR TEXT (the href is a useless wp-anchor).
#
# This is a DIFFERENT SOURCE LINEAGE than the DITA store, so some text legitimately
# differs at the 2005<->2021 seam (e.g. '15 U.S.C. 637' vs the DITA's 'U.S.C.637').
# The parser therefore targets the chunker's CONVENTIONS -- line grouping, label
# handling, title period-stripping, [IMAGE: id], table->HTML -- not byte-identity
# with a different generator. Within the 2005 era (one generator) consecutive
# editions stay self-consistent, so backfill there is clean; the one cross-source
# seam is a documented, one-time batch of rendering-difference "changes".

WW_INDENT = re.compile(r"\bpIndented(\d+)\b")
WW_SECTION_NUM = re.compile(r"^\s*(\d+\.\d+(?:-\d+)*)\s+(.*)$", re.S)
WW_SUBPART = re.compile(r"Subpart\s+(\d+\.\d+)\s*[-–—]\s*(.*)$", re.S)


def _ww_is_nav(el):
    """A page-navigation table (Previous/Next buttons) — id='SummaryNotReq…' or holding
    only navprev/navnext gifs. Rendered on every webworks page; not FAR content."""
    if el.tag != "table":
        return False
    if (el.get("id") or "").startswith("SummaryNotReq"):
        return True
    srcs = [i.get("src") or "" for i in el.findall(".//img")]
    return bool(srcs) and all(re.search(r"nav(prev|next)\.gif", s) for s in srcs)


def _ww_depth(el):
    """Structural depth of a webworks body paragraph from its class: pBody / pBodyCtr*
    = 0 (top level), pIndentedN = N. None for non-paragraph blocks."""
    if el.tag != "p":
        return None
    cls = el.get("class") or ""
    m = WW_INDENT.search(cls)
    if m:
        return int(m.group(1))
    return 0 if cls.startswith("pBody") else 0


def _ww_is_centered(el):
    """A centered clause line (dated SmCaps title, End marker): class pBodyCtr…."""
    return el.tag == "p" and (el.get("class") or "").startswith("pBodyCtr")


def _ww_flatten_p(p):
    """Flatten a webworks <p> to text, mirroring the chunker's conventions: <em>/<a>
    contribute their text, <a name> anchors and <br> collapse to nothing/space, images
    become '[IMAGE: id]'. Labels are already literal in the text, so (unlike the DITA
    flatten) nothing is injected."""
    parts = [p.text or ""]
    for ch in p:
        if not isinstance(ch.tag, str):
            if ch.tail:
                parts.append(ch.tail)
            continue
        if ch.tag in ("dt", "dd"):
            break                                     # unclosed-tag artifact: next paragraph
        if ch.tag == "img":
            parts.append(" [IMAGE: " + X.img_id(ch.get("src") or "") + "] ")
        elif ch.tag == "a" and ch.get("name") and not (ch.get("href")):
            pass                                      # position anchor: no text
        elif ch.tag == "br":
            parts.append(" ")
        else:
            parts.append(ch.text_content())
        if ch.tail:
            parts.append(ch.tail)
    return X.norm("".join(parts))


def _ww_table_html(t):
    """Render a webworks <table> (<tr><th|td><p class='pCell…'>…) to the chunker's
    minimal <table><caption><thead><tr><th>… serialization."""
    cap_el = t.find("caption")
    cap = X._esc(X.norm(_text(cap_el))) if cap_el is not None else ""
    rows = t.findall(".//tr")
    def cells(tr, tag):
        out = []
        for c in tr:
            if c.tag not in ("td", "th"):
                continue
            sp = c.get("colspan")
            sp = int(sp) if sp and sp.isdigit() else 1
            attr = f' colspan="{sp}"' if sp > 1 else ""
            out.append(f"<{tag}{attr}>{X._esc(X.norm(_text(c)))}</{tag}>")
        return "<tr>" + "".join(out) + "</tr>"
    head = [tr for tr in rows if tr.find(".//th") is not None]
    bod = [tr for tr in rows if tr.find(".//th") is None]
    parts = ["<table>"]
    if cap:
        parts.append(f"<caption>{cap}</caption>")
    if head:
        parts.append("<thead>" + "".join(cells(tr, "th") for tr in head) + "</thead>")
    if bod:
        parts.append("<tbody>" + "".join(cells(tr, "td") for tr in bod) + "</tbody>")
    parts.append("</table>")
    return "".join(parts)


def _ww_flatten_block(el):
    """One webworks block -> flattened item(s). Tables render inline; images -> token."""
    if el.tag in ("p", "dt", "dd", "div") or re.match(r"h[1-6]$", el.tag or ""):
        t = _ww_flatten_p(el)
        return [t] if t else []
    if el.tag == "table":
        return [_ww_table_html(el)]
    if el.tag == "img":
        return ["[IMAGE: " + X.img_id(el.get("src") or "") + "]"]
    return []


def _ww_label(text):
    """Leading paragraph label token of a flattened line ('(a)…' -> 'a', '(a)(1)…' ->
    'a'); '' when the line doesn't start with a parenthesized label."""
    m = re.match(r"\(([A-Za-z0-9]+)\)", text or "")
    return X.tok(m.group(1)) if m else ""


def _ww_render_scope(ps):
    """Plain text of a webworks paragraph sequence + every <a> anchor's [start,end)
    span and href. Mirrors X.render_scope so the reference windows line up."""
    parts, xpos = [], []
    L = lambda: sum(len(z) for z in parts)
    def ser(node):
        if node.text:
            parts.append(node.text)
        for ch in node:
            if not isinstance(ch.tag, str):
                if ch.tail:
                    parts.append(ch.tail)
                continue
            if ch.tag in ("dt", "dd"):
                break                                 # unclosed-tag artifact: separate paragraph
            if ch.tag == "a" and ch.get("href"):
                s = L(); parts.append(ch.text_content()); xpos.append((ch, s, L()))
            elif ch.tag == "a" and ch.get("name"):
                pass
            elif ch.tag == "img":
                parts.append(" [IMAGE: " + X.img_id(ch.get("src") or "") + "] ")
            else:
                ser(ch)
            if ch.tail:
                parts.append(ch.tail)
    for p in ps:
        ser(p); parts.append(" ")
    return "".join(parts), xpos


def _ww_collect_refs(ps, sec_num):
    """Internal FAR cross-refs for a webworks unit. The citation is the ANCHOR TEXT
    (e.g. '5.201', '14.201-6'); the href is a wp-anchor we ignore. Plus ranges and
    'of this section', same as the DITA path."""
    text, xpos = _ww_render_scope(ps)
    refs = []
    for el, s, e in xpos:
        href = el.get("href") or ""
        if href.startswith(("http", "mailto")):
            continue
        anchor = X.norm(text[s:e])
        m = re.match(r"(?:subpart\s+)?(\d+\.\d+(?:-\d+)?)", anchor, re.I)
        if not m:
            continue
        base = m.group(1)
        q = re.match(r"\s*((?:\([A-Za-z0-9]+\))+)", text[e:e + 40])
        endref = e + (q.end() if q else 0)
        a, b = max(0, s - X.WB), min(len(text), endref + X.WA)
        lit = f'<xref href="{href}">{text[s:e]}</xref>'
        ctx = ("…" if a > 0 else "") + X.norm(text[a:s] + lit + text[e:b]) + ("…" if b < len(text) else "")
        target = base + (q.group(1) if q else "")
        refs.append({"kind": "inferred" if q else "explicit", "target": target,
                     "alternate": "", "evidence": ctx})
    refs.extend(X._range_refs(text, sec_num))
    for m in re.finditer(r"paragraphs?\s+(\([a-z0-9]+\)(?:\([a-z0-9]+\))*)\s+of this section", text):
        refs.append({"kind": "inferred", "target": sec_num + m.group(1),
                     "evidence": X._window(text, max(0, m.start() - X.WB),
                                           min(len(text), m.end() + X.WA))})
    return X._group_refs(refs)


def _ww_collect_external(ps):
    """External (USC/CFR/EO/form/URL) refs for a webworks unit."""
    text, xpos = _ww_render_scope(ps)
    out, index = [], {}
    def add(r, ev, kind, href=""):
        key = (r["target"], r.get("locator", ""))
        if key not in index:
            index[key] = len(out)
            out.append({"ref_type": r["ref_type"], "target": r["target"],
                        "locator": r.get("locator", ""),
                        "node_label": r.get("node_label", r["target"]),
                        "division_levels": r.get("division_levels", []),
                        "citation": r.get("citation", ""),
                        "href": href, "confidence": "explicit", "mentions": []})
        e = out[index[key]]
        if href and not e["href"]:
            e["href"] = href
        if ev and not any(m["evidence"] == ev for m in e["mentions"]):
            e["mentions"].append({"kind": kind, "evidence": ev})
    for rx, build in X.EXTERNAL_PATTERNS:
        for m in rx.finditer(text):
            r = build(m); r["citation"] = X.norm(m.group(0))
            add(r, X._window(text, max(0, m.start() - X.WB), min(len(text), m.end() + X.WA)),
                "inferred")
    for el, s, e in xpos:
        href = el.get("href") or ""
        if not href.startswith("http"):
            continue
        anchor = X.norm(text[s:e])
        ev = X._window(text, max(0, s - X.WB), min(len(text), e + X.WA))
        r = X.parse_external(anchor) or X.parse_form(anchor)
        if r:
            r.setdefault("citation", anchor)
            add(r, ev, "explicit", href)
        else:
            add({"ref_type": "url", "target": href.rstrip("/"), "node_label": anchor or href,
                 "division_levels": [], "locator": "", "citation": anchor or href},
                ev, "explicit", href)
    return out


def _ww_sections(body):
    """Split a webworks subpart/clause file body into sections. Returns
    [{num, title, blocks}] -- one per <h3 class='pSection'>; blocks are the following
    content paragraphs/tables up to the next section. Also returns the subpart title."""
    subpart_num, subpart_title = "", ""
    sections, cur = [], None
    for el in body:
        if not isinstance(el.tag, str):
            continue
        cls = el.get("class") or ""
        if el.tag == "h2" and "pSubpart" in cls:
            m = WW_SUBPART.search(X.norm(el.text_content()))
            if m:
                subpart_num, subpart_title = m.group(1), m.group(2).rstrip(".").strip()
                cur = None
            elif cur is not None:
                # NOT a subpart head: webworks styles the '* * * * *' elision separator
                # inside clause Alternates as pSubpart -- it's content, and treating it
                # as a boundary silently dropped everything after it (Alternates III+ of
                # the advance-payment clauses). Caught by the corpus_audit conservation check.
                cur["blocks"].append(el)
            continue
        if el.tag == "h3" and "pSection" in cls:
            m = WW_SECTION_NUM.match(X.norm(el.text_content()))
            if m:
                title = m.group(2).strip()
                title = title[:-1].rstrip() if title.endswith(".") else title
                cur = {"num": m.group(1), "title": title, "blocks": []}
                sections.append(cur)
            else:
                cur = None
            continue
        if el.tag in ("p", "table", "img") and cur is not None:
            if el.tag == "p" and not (el.get("class") or "").startswith("p"):
                continue
            if _ww_is_nav(el):                        # trailing Previous/Next nav table
                continue
            cur["blocks"].append(el)
    return sections, subpart_num, subpart_title


def _ww_find_end_and_alt(blocks):
    """(end_marker, end_index, alt_start_index) for a clause's block list, mirroring the
    chunker: the first centered '(End of clause/provision)' line, and the first following
    paragraph that opens an alternate ('Alternate I …')."""
    end_text, end_idx = "", -1
    for i, el in enumerate(blocks):
        if el.tag != "p":
            continue
        em = _end_marker(_ww_flatten_p(el), _cls(el))
        if em:
            end_text, end_idx = em, i; break
    alt_idx = -1
    for i in range(end_idx + 1 if end_idx >= 0 else 0, len(blocks)):
        if blocks[i].tag == "p" and CK.ALT_OPENER.match(_ww_flatten_p(blocks[i])):
            alt_idx = i; break
    return end_text, end_idx, alt_idx


def _ww_group(blocks, row_labels=None):
    """Group a block list into lines by structural depth: a depth-0 paragraph (pBody)
    or a table/image starts a new line; deeper pIndented paragraphs attach to the open
    line (space-joined) -- the chunker's flatten_li behavior. Returns [{label,is_row,blocks}].

    is_row marks the top-level DITA <li>s that become paragraph chunks. The FrameMaker
    source occasionally mis-renders a NESTED list item at pBody depth (e.g. a few of the
    numbered sub-items inside 2.101 definitions), which would fabricate a spurious/
    duplicate top-level row. When `row_labels` (the store's known paragraph labels for
    this unit) is given, a depth-0 label is a row only if the store agrees; a match is
    consumed so a repeated label (real second occurrence) still lands correctly, and an
    unmatched label attaches to the open line instead. Without row_labels every labelled
    depth-0 paragraph is a row (the standalone fallback)."""
    known = row_labels is not None
    remaining = dict(row_labels) if known else {}     # label -> count still expected
    groups, open_g = [], None
    for el in blocks:
        d = _ww_depth(el)
        if el.tag in ("table", "img"):
            open_g = None
            groups.append({"label": "", "is_row": False, "blocks": [el]})
            continue
        if el.tag != "p":
            continue
        t = _ww_flatten_p(el)
        if not t:
            continue
        if d == 0:
            label = _ww_label(t)
            is_row = bool(label) and (remaining.get(label, 0) > 0 if known else True)
            if is_row and known:
                remaining[label] -= 1
            if is_row or not label or open_g is None:
                open_g = {"label": label if is_row else "", "is_row": is_row, "blocks": [el]}
                groups.append(open_g)
            else:
                open_g["blocks"].append(el)      # stray mis-rendered nested item: attach
        elif open_g is not None:
            open_g["blocks"].append(el)
        else:
            open_g = {"label": "", "is_row": False, "blocks": [el]}
            groups.append(open_g)
    return groups


def _ww_group_text(g):
    return " ".join(s for el in g["blocks"] for s in _ww_flatten_block(el))


def _ww_meta(blocks, end_text):
    """(instrument, date, prescribed_by) for a webworks clause unit, mirroring the
    chunker: instrument from the end marker or an 'insert the following clause/provision'
    line; date from the centered SmCaps dated title; prescribed_by from 'As prescribed in'."""
    instrument = ("clause" if end_text == "(End of clause)"
                  else "provision" if end_text == "(End of provision)" else "")
    if not instrument:
        # no end marker in this edition: the chunker's exact fallback -- the prefatory
        # 'insert the following clause|provision' line. Kept tight; broader phrasings
        # ('insert a clause', '… substantially the same') also match ordinary sections
        # that merely PRESCRIBE a clause, which are not themselves clauses.
        for el in blocks[:3]:
            low = _ww_flatten_p(el).lower() if el.tag == "p" else ""
            if "following clause" in low:
                instrument = "clause"; break
            if "following provision" in low:
                instrument = "provision"; break
    date, prescribed_by = "", ""
    if instrument:
        for el in blocks[:5]:
            if el.tag != "p":
                continue
            t = _ww_flatten_p(el)
            if not prescribed_by:
                m = CK.PRESCRIBED.search(t)
                if m:
                    prescribed_by = m.group(1).replace(" ", "")
            if not date and "SmCaps" in (el.get("class") or ""):
                dm = CK.ALT_DATE.search(t)
                if dm:
                    date = X.norm(f"{dm.group(1)} {dm.group(2)}")
    return instrument, date, prescribed_by


def _ww_build_unit(sec, cfg, row_labels=None):
    """Build the chunk rows for one webworks section. Returns [] when near-empty.
    row_labels: {label: count} of the store's known paragraph rows for this unit (from
    hints), used to reject FrameMaker-mis-rendered stray nested items; None = standalone."""
    num, title, blocks = sec["num"], sec["title"], sec["blocks"]
    reg = cfg["regulation"]
    url = cfg["url_template"].format(num=num)
    base_type = "subsection" if "-" in num else "section"
    end_text, end_idx, alt_idx = _ww_find_end_and_alt(blocks)
    instrument, bdate, bpresc = _ww_meta(blocks, end_text)

    # base blocks: everything except the End marker line and the trailing alternates
    cut = alt_idx if alt_idx >= 0 else len(blocks)
    base_blocks = [b for i, b in enumerate(blocks)
                   if i < cut and i != end_idx]

    def row(number, typ, tokens, ps, text, scan_blocks, alternate="",
            date="", prescribed_by="", end_marker=""):
        r = {"citation": f"{reg}-{number}", "regulation": reg,
             "source_version": cfg.get("source_version", ""),
             "pipeline_version": cfg.get("pipeline_version", ""),
             "type": typ, "instrument": instrument, "alternate": alternate,
             "part_title": "", "subpart_title": "",
             "section_title": "", "subsection_title": ""}
        r.update(CK.decompose(num, tokens, ["paragraph"][:cfg["bottom_depth"]]))
        r["url"] = url
        r["cross_references"] = _ww_collect_refs(ps, num)
        r["external_references"] = _ww_collect_external(ps)
        imgs = []
        for el in scan_blocks:
            for im in ([el] if el.tag == "img" else el.findall(".//img")):
                iid = X.img_id(im.get("src") or "")
                if iid not in imgs:
                    imgs.append(iid)
        r["images"] = imgs
        r["changes"] = []
        r["date"] = date
        r["prescribed_by"] = prescribed_by
        r["reserved"] = X.norm(text).rstrip(".").lower().endswith("[reserved]")
        r["end_marker"] = end_marker
        r["text"] = text
        return r

    groups = _ww_group(base_blocks, row_labels)
    unit_text = "\n".join(t for t in (_ww_group_text(g) for g in groups) if t)
    unit_ps = [el for g in groups for el in g["blocks"] if el.tag == "p"]
    rows = [row(num, base_type, [], unit_ps, unit_text, base_blocks,
                date=bdate, prescribed_by=bpresc, end_marker=end_text)]

    if cfg["bottom_depth"] >= 1:
        for g in groups:
            if g["is_row"]:
                cit = f"{num}({g['label']})"
                gps = [el for el in g["blocks"] if el.tag == "p"]
                rows.append(row(cit, CK.level_name(1), [g["label"]], gps,
                                _ww_group_text(g), g["blocks"]))

    # alternates: paragraphs after the End marker, split into a span per
    # 'Alternate <roman>' opener; each becomes its own sibling row (like the base
    # clause but distinguished by `alternate`).
    if alt_idx >= 0:
        spans, cur = [], None
        for el in blocks[alt_idx:]:
            m = CK.ALT_OPENER.match(_ww_flatten_p(el)) if el.tag == "p" else None
            if m:
                cur = {"roman": m.group(1).upper(), "blocks": [el]}
                spans.append(cur)
            elif cur is not None:
                cur["blocks"].append(el)
        for sp in spans:
            aps = [el for el in sp["blocks"] if el.tag == "p"]
            atext = " ".join(s for el in sp["blocks"] for s in _ww_flatten_block(el))
            lead = _ww_flatten_p(sp["blocks"][0]).split(".", 1)[0]
            adm = CK.ALT_DATE.search(lead)
            apm = CK.PRESCRIBED.search(_ww_flatten_p(sp["blocks"][0]))
            rows.append(row(num, base_type, [], aps, atext, sp["blocks"],
                            alternate=CK.roman_to_arabic(sp["roman"]),
                            date=X.norm(f"{adm.group(1)} {adm.group(2)}") if adm else "",
                            prescribed_by=apm.group(1).replace(" ", "") if apm else ""))

    own = "subsection_title" if "-" in num else "section_title"
    for r in rows:
        r[own] = title
    if len(rows) == 1 and len(unit_text) < cfg.get("min_text", MIN_TEXT):
        return []
    return rows


def _ww_files(root):
    """Content HTML files of a webworks edition, in FAR order: Subpart N_M.html plus the
    52_* clause groups; skip TOC/cover/forms/matrix/nav pages. Dedupes files that map to the
    same subpart/clause group -- some editions ship both a space- and an underscore-named
    copy of a subpart (e.g. 'Subpart 5_6.html' AND 'Subpart_5_6.html'), which would parse
    the same sections twice."""
    seen, out = {}, []
    for f in sorted(glob.glob(os.path.join(root, "*.html"))):
        b = os.path.basename(f)
        if not (re.match(r"Subpart[ _]\d+_\d+", b) or re.match(r"52_\d", b)
                or re.match(r"Part \d+_?\w*\.html", b)):
            continue
        norm = re.sub(r"Subpart[ _]", "Subpart_", b)      # collapse space/underscore variants
        if norm in seen:
            continue
        seen[norm] = True
        out.append(f)
    def key(f):
        b = os.path.basename(f)
        m = re.search(r"(\d+)[ _](\d+)", b)
        return (int(m.group(1)), int(m.group(2))) if m else (999, 999)
    return sorted(out, key=key)


def _ww_row_labels(hints):
    """{unit_num: {label: count}} of the store's paragraph rows per unit, from hints —
    used to reject FrameMaker-mis-rendered stray nested items (see _ww_group)."""
    out = {}
    for num, hu in (hints.get("units") or {}).items():
        counts = {}
        for line in hu.get("lines", []):
            for r in line.get("rows", []):
                counts[r["l"]] = counts.get(r["l"], 0) + 1
        if counts:
            out[num] = counts
    return out


def chunk_edition_webworks2005(edition_dir, cfg, hints=None):
    """All chunk rows for one webworks-2005 archive edition folder."""
    hints = hints or {}
    root = ww_root(edition_dir)
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    row_labels_map = _ww_row_labels(hints)
    known_units = set(hints.get("units") or {})       # every unit the store knows
    rows, manifest = [], {"processed": [], "skipped": []}
    for f in _ww_files(root):
        try:
            doc = lxml.html.parse(f).getroot()
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f), "reason": repr(e)[:60]})
            continue
        body = doc.find(".//body")
        if body is None:
            continue
        sections, sp_num, sp_title = _ww_sections(body)
        if sp_num and sp_title:
            subpart_titles.setdefault(sp_num, sp_title)
        for sec in sections:
            # unit known to the store -> use its row-label counts ({} = store has no
            # paragraph rows there, so reject every stray label); 2005-only units the
            # store never saw fall back to standalone class-depth rules (rl=None)
            rl = (row_labels_map.get(sec["num"], {}) if sec["num"] in known_units
                  else None)
            try:
                r = _ww_build_unit(sec, cfg, rl)
            except Exception as e:
                manifest["skipped"].append({"file": sec["num"], "reason": repr(e)[:70]})
                continue
            if r:
                rows.extend(r)
                manifest["processed"].append(sec["num"])
            else:
                manifest["skipped"].append({"file": sec["num"], "reason": "near-empty"})
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ---------------------------------------------------------------- webworks-parts era
# GSAM's own 2004-2019 site render: WebWorks-family XHTML with ONE FILE PER PART
# ('html/Part501.html'), headings as <h2 class="pHeading1"> (Part) /
# <h3 class="pHeading2"> (Subpart) / <h4 class="pHeading3"> (section), paragraphs in
# the webworks-2005 vocabulary (pBody/pIndentedN/pCell*) but wrapped in <div
# class="pDefault"> containers -- so the walk is descendant-order, not direct
# children. Reuses the whole _ww_* machinery.

WWP_PART = re.compile(r"^\s*Part\s+(\d+)\s*(?:--|—|–|-)?\s*(.*)$", re.I)


def _wp_files(edition_dir):
    """Per-part WebWorks render files (GSAM 'html/Part501.html', 'Part552_Sub2B.html')."""
    return sorted(f for f in glob.glob(os.path.join(edition_dir, "**", "*.htm*"),
                                       recursive=True)
                  if re.match(r"(?i)^part\d+", os.path.basename(f))
                  and "__MACOSX" not in f)


def _wp_sections(body):
    """Split a per-part WebWorks render body into sections. Returns (sections,
    {part_num: title}, {subpart_num: title}); each section is {num, title, blocks}.
    Sole source of truth for BOTH the chunker and the completeness manifest."""
    sections, cur, part_t, sub_t = [], None, {}, {}
    for el in body.iter():
        if not isinstance(el.tag, str):
            continue
        cls = el.get("class") or ""
        if cls.startswith("pHeading"):
            t = X.norm(el.text_content())
            m = WW_SECTION_NUM.match(t)
            if "pHeading3" in cls and m and NUMERIC.match(m.group(1)):
                title = m.group(2).strip()
                title = title[:-1].rstrip() if title.endswith(".") else title
                cur = {"num": m.group(1), "title": title, "blocks": []}
                sections.append(cur)
                continue
            sm = WW_SUBPART.search(t)
            pm = WWP_PART.match(t)
            if sm:
                sub_t.setdefault(sm.group(1), sm.group(2).rstrip(".").strip())
            elif pm and pm.group(2):
                part_t.setdefault(pm.group(1), pm.group(2).rstrip(".").strip())
            cur = None
            continue
        if cur is None:
            continue
        # this render wraps every SECTION in a layout <table> (td holds the pHeading3
        # + its paragraphs), while real data tables carry pCell* paragraphs. So: a p is
        # content unless it is a data-table cell; a table is a block only when it IS a
        # data table (layout tables are transparent -- inner paragraphs walked directly).
        if el.tag == "p":
            if not cls.startswith("p") or cls.startswith("pCell"):
                continue
            if _ww_is_nav(el):
                continue
            cur["blocks"].append(el)
        elif el.tag == "table":
            def _is_data(t):
                return bool(t.xpath('.//p[starts-with(@class, "pCell")]')) \
                    and not t.xpath('.//*[starts-with(@class, "pHeading")]')
            if _is_data(el) and not _ww_is_nav(el) \
                    and not any(a.tag == "table" and _is_data(a)
                                for a in el.iterancestors()):
                cur["blocks"].append(el)
        elif el.tag == "img":
            if not any(a.tag == "table" and a.xpath(
                    './/p[starts-with(@class, "pCell")]')
                    for a in el.iterancestors()):
                cur["blocks"].append(el)
    return sections, part_t, sub_t


def chunk_edition_webworks_parts(edition_dir, cfg, hints=None):
    """All chunk rows for one per-part WebWorks render (GSAM 2004-2019)."""
    hints = hints or {}
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    row_labels_map = _ww_row_labels(hints)
    known_units = set(hints.get("units") or {})
    rows, manifest = [], {"processed": [], "skipped": []}
    for f in _wp_files(edition_dir):
        try:
            doc = lxml.html.parse(f).getroot()
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f),
                                        "reason": repr(e)[:60]})
            continue
        body = doc.find(".//body")
        if body is None:
            continue
        sections, part_t, sub_t = _wp_sections(body)
        for k, v in part_t.items():
            part_titles.setdefault(k, v)
        for k, v in sub_t.items():
            subpart_titles.setdefault(k, v)
        for sec in sections:
            rl = (row_labels_map.get(sec["num"], {}) if sec["num"] in known_units
                  else None)
            try:
                r = _ww_build_unit(sec, cfg, rl)
            except Exception as e:
                manifest["skipped"].append({"file": sec["num"], "reason": repr(e)[:70]})
                continue
            if r:
                rows.extend(r)
                manifest["processed"].append(sec["num"])
            else:
                manifest["skipped"].append({"file": sec["num"], "reason": "near-empty"})
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ================================================================ WEBWORKS-2001 ERA
# Earlier Quadralay WebWorks HTML 4.0 (the "FAC 1997-27n" .. "FAC 2001-xx" folders,
# ~2001-2005). Same per-SUBPART granularity as webworks-2005, but a different markup
# dialect: content sits inside a <blockquote>; a section opens with <h2 class="Heading1">
# (subpart heads are <h3 class="Heading2">); unlabelled section text is <p class="Body">;
# paragraph lists are <dt class="IndentedN"> items inside a <dl> -- with N OFFSET BY ONE
# from 2005 (Indented1 = the top-level (a), Indented2 = (1), Indented3 = (i)). A clause's
# dated title and its "(End of clause/provision)" marker are center-styled <div>s (inline
# style, no class). There are NO internal cross-reference links in this era -- references
# are plain prose -- so cross_references come only from ranges / "of this section" (which
# don't affect content_hash anyway). Reuses the webworks-2005 flatten/table/label/ref
# helpers; only the structure walk differs.

W1_INDENT = re.compile(r"\bIndented(\d+)\b")


def _w1_depth(el):
    """Structural depth of a webworks-2001 paragraph: dt.IndentedN -> N-1 (Indented1 = the
    top-level (a) row); Body / center divs / bare dt -> 0."""
    m = W1_INDENT.search(el.get("class") or "")
    return int(m.group(1)) - 1 if m else 0


def _w1_is_center(el):
    """A center-styled <div>: a clause's dated title or its End marker."""
    return el.tag == "div" and "text-align: center" in (el.get("style") or "")


def _w1_sections(container):
    """Split a webworks-2001 subpart/clause body into sections. Returns (sections, subpart
    number, subpart title); each section is {num, title, blocks} with blocks the flattened
    sequence of content elements (Body <p>, the <dt> items of each <dl>, center <div>s,
    tables) up to the next section/subpart heading."""
    subpart_num, subpart_title = "", ""
    sections, cur = [], None
    for el in container:
        if not isinstance(el.tag, str):
            continue
        cls = el.get("class") or ""
        if "Heading2" in cls or (el.tag == "h3" and "Heading" in cls):        # subpart head
            m = WW_SUBPART.search(X.norm(el.text_content()))
            if m:
                subpart_num, subpart_title = m.group(1), m.group(2).rstrip(".").strip()
                cur = None
            elif cur is not None:
                cur["blocks"].append(el)     # elision separator etc. -- content, not a boundary
            continue
        if "Heading1" in cls or (el.tag == "h2" and "Heading" in cls):        # section head
            m = WW_SECTION_NUM.match(X.norm(el.text_content()))
            if m:
                title = m.group(2).strip()
                title = title[:-1].rstrip() if title.endswith(".") else title
                cur = {"num": m.group(1), "title": title, "blocks": []}
                sections.append(cur)
            else:
                cur = None
            continue
        if cur is None:
            continue
        if el.tag == "dl":
            # <dt>/<dd> omit their close tags, so lxml NESTS them; iter() recovers the
            # true document-order sequence, and the flatten helpers stop at nested dt/dd.
            cur["blocks"].extend(e for e in el.iter() if e.tag in ("dt", "dd"))
        elif el.tag in ("p", "div", "table"):
            if _ww_is_nav(el):
                continue
            cur["blocks"].append(el)
    return sections, subpart_num, subpart_title


def _w1_find_end_and_alt(blocks):
    """(end_marker, end_index, alt_start_index) mirroring the chunker: the center-div (or
    any element) reading '(End of clause/provision)', and the first following paragraph
    that opens an alternate ('Alternate I …')."""
    end_text, end_idx = "", -1
    for i, el in enumerate(blocks):
        em = _end_marker(_ww_flatten_p(el), _cls(el))
        if em:
            end_text, end_idx = em, i; break
    alt_idx = -1
    for i in range(end_idx + 1 if end_idx >= 0 else 0, len(blocks)):
        if CK.ALT_OPENER.match(_ww_flatten_p(blocks[i])):
            alt_idx = i; break
    return end_text, end_idx, alt_idx


def _w1_meta(blocks, end_text):
    """(instrument, date, prescribed_by) for a webworks-2001 clause: instrument from the
    end marker or the 'insert the following clause|provision' Body line; date from the
    center-div dated title '(Mon YYYY)'; prescribed_by from 'As prescribed in'."""
    instrument = ("clause" if end_text == "(End of clause)"
                  else "provision" if end_text == "(End of provision)" else "")
    if not instrument:
        for el in blocks[:3]:
            low = _ww_flatten_p(el).lower()
            if "following clause" in low:
                instrument = "clause"; break
            if "following provision" in low:
                instrument = "provision"; break
    date, prescribed_by = "", ""
    if instrument:
        for el in blocks[:5]:
            t = _ww_flatten_p(el)
            if not prescribed_by:
                m = CK.PRESCRIBED.search(t)
                if m:
                    prescribed_by = m.group(1).replace(" ", "")
            if not date and _w1_is_center(el):            # the dated title div
                dm = CK.ALT_DATE.search(t)
                if dm:
                    date = X.norm(f"{dm.group(1)} {dm.group(2)}")
    return instrument, date, prescribed_by


def _w1_group(blocks, row_labels=None):
    """Group by structural depth: a depth-0 element (Body / center div / dt.Indented1)
    or a table starts a new line; deeper dt.IndentedN attach to the open line. is_row marks
    the labelled top-level (a) items. row_labels (optional store hints) reject stray labels,
    same as _ww_group."""
    known = row_labels is not None
    remaining = dict(row_labels) if known else {}
    groups, open_g = [], None
    for el in blocks:
        if el.tag == "table":
            open_g = None
            groups.append({"label": "", "is_row": False, "blocks": [el]})
            continue
        t = _ww_flatten_p(el)
        if not t:
            continue
        d = _w1_depth(el)
        if d <= 0:
            label = _ww_label(t) if el.tag == "dt" else ""
            is_row = bool(label) and (remaining.get(label, 0) > 0 if known else True)
            if is_row and known:
                remaining[label] -= 1
            if is_row or not label or open_g is None:
                open_g = {"label": label if is_row else "", "is_row": is_row, "blocks": [el]}
                groups.append(open_g)
            else:
                open_g["blocks"].append(el)
        elif open_g is not None:
            open_g["blocks"].append(el)
        else:
            open_g = {"label": "", "is_row": False, "blocks": [el]}
            groups.append(open_g)
    return groups


def _w1_build_unit(sec, cfg, row_labels=None):
    """Build the chunk rows for one webworks-2001 section."""
    num, title, blocks = sec["num"], sec["title"], sec["blocks"]
    reg = cfg["regulation"]
    url = cfg["url_template"].format(num=num)
    base_type = "subsection" if "-" in num else "section"
    end_text, end_idx, alt_idx = _w1_find_end_and_alt(blocks)
    instrument, bdate, bpresc = _w1_meta(blocks, end_text)
    cut = alt_idx if alt_idx >= 0 else len(blocks)
    base_blocks = [b for i, b in enumerate(blocks) if i < cut and i != end_idx]

    def row(number, typ, tokens, ps, text, scan_blocks, alternate="",
            date="", prescribed_by="", end_marker=""):
        r = {"citation": f"{reg}-{number}", "regulation": reg,
             "source_version": cfg.get("source_version", ""),
             "pipeline_version": cfg.get("pipeline_version", ""),
             "type": typ, "instrument": instrument, "alternate": alternate,
             "part_title": "", "subpart_title": "",
             "section_title": "", "subsection_title": ""}
        r.update(CK.decompose(num, tokens, ["paragraph"][:cfg["bottom_depth"]]))
        r["url"] = url
        r["cross_references"] = _ww_collect_refs(ps, num)
        r["external_references"] = _ww_collect_external(ps)
        imgs = []
        for el in scan_blocks:
            for im in ([el] if el.tag == "img" else el.findall(".//img")):
                iid = X.img_id(im.get("src") or "")
                if iid not in imgs:
                    imgs.append(iid)
        r["images"] = imgs
        r["changes"] = []
        r["date"] = date
        r["prescribed_by"] = prescribed_by
        r["reserved"] = X.norm(text).rstrip(".").lower().endswith("[reserved]")
        r["end_marker"] = end_marker
        r["text"] = text
        return r

    groups = _w1_group(base_blocks, row_labels)
    unit_text = "\n".join(t for t in (_ww_group_text(g) for g in groups) if t)
    unit_ps = [el for g in groups for el in g["blocks"]]
    rows = [row(num, base_type, [], unit_ps, unit_text, base_blocks,
                date=bdate, prescribed_by=bpresc, end_marker=end_text)]

    if cfg["bottom_depth"] >= 1:
        for g in groups:
            if g["is_row"]:
                rows.append(row(f"{num}({g['label']})", CK.level_name(1), [g["label"]],
                                g["blocks"], _ww_group_text(g), g["blocks"]))

    if alt_idx >= 0:
        spans, curspan = [], None
        for el in blocks[alt_idx:]:
            m = CK.ALT_OPENER.match(_ww_flatten_p(el))
            if m:
                curspan = {"roman": m.group(1).upper(), "blocks": [el]}
                spans.append(curspan)
            elif curspan is not None:
                curspan["blocks"].append(el)
        for sp in spans:
            atext = " ".join(s for el in sp["blocks"] for s in _ww_flatten_block(el))
            lead = _ww_flatten_p(sp["blocks"][0]).split(".", 1)[0]
            adm = CK.ALT_DATE.search(lead)
            apm = CK.PRESCRIBED.search(_ww_flatten_p(sp["blocks"][0]))
            rows.append(row(num, base_type, [], sp["blocks"], atext, sp["blocks"],
                            alternate=CK.roman_to_arabic(sp["roman"]),
                            date=X.norm(f"{adm.group(1)} {adm.group(2)}") if adm else "",
                            prescribed_by=apm.group(1).replace(" ", "") if apm else ""))

    own = "subsection_title" if "-" in num else "section_title"
    for r in rows:
        r[own] = title
    if len(rows) == 1 and len(unit_text) < cfg.get("min_text", MIN_TEXT):
        return []
    return rows


def chunk_edition_webworks2001(edition_dir, cfg, hints=None):
    """All chunk rows for one webworks-2001 archive edition folder."""
    hints = hints or {}
    root = ww_root(edition_dir)
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    row_labels_map = _ww_row_labels(hints)
    known_units = set(hints.get("units") or {})
    rows, manifest = [], {"processed": [], "skipped": []}
    for f in _ww_files(root):
        try:
            doc = lxml.html.parse(f).getroot()
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f), "reason": repr(e)[:60]})
            continue
        container = doc.find(".//blockquote")
        if container is None:
            container = doc.find(".//body")
        if container is None:
            continue
        sections, sp_num, sp_title = _w1_sections(container)
        if sp_num and sp_title:
            subpart_titles.setdefault(sp_num, sp_title)
        for sec in sections:
            rl = (row_labels_map.get(sec["num"], {}) if sec["num"] in known_units else None)
            try:
                r = _w1_build_unit(sec, cfg, rl)
            except Exception as e:
                manifest["skipped"].append({"file": sec["num"], "reason": repr(e)[:70]})
                continue
            if r:
                rows.extend(r)
                manifest["processed"].append(sec["num"])
            else:
                manifest["skipped"].append({"file": sec["num"], "reason": "near-empty"})
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ================================================================ LEGACY ERA (1995-2002)
# Hand-authored / early-tool plain HTML (the "FAC 1990-34" .. "FAC 1997-27" folders plus
# "1997 Reissue"). One file per PART -- html/05.html (1997 series), html/5.html (early
# 1990s), html/05PART.HTM (PageMill editions) -- plus 52_NNN.html clause groups in later
# editions. NO CSS classes at all: a section opens with <Hn><B>5.101  Title.</B> (1997
# variant) or a <P> whose only child is <B>5.101  Title.</B> (1990 variant); subparts are
# centered bold "SUBPART 5.1--DISSEMINATION ..." (often ALL CAPS); files begin with a TOC
# (tables of #anchor links -- dropped, since no section is open yet). Paragraphs are flat
# <P>s with literal labels and NO depth markers whatsoever; cross-references are plain
# prose (never links). Clause dated titles are centered <P>s; End markers are text.
#
# Row (depth-1) detection without depth markers: hints decide for units the store knows
# (same mechanism as the other eras). For dead units the FAR's own label ladder decides:
# depth 1 is ALWAYS lowercase letters ((a),(b),(c)...); digits/uppercase/multi-char romans
# are nested. The one ambiguity is single-char roman-lookalikes ((i),(v),(x)...): they are
# rows only when they continue the letter ladder (previous row was (h) -> (i) is letter i).

LG_SUBPART = re.compile(r"^\s*SUBPART\s+(\d+\.\d+)\s*(?:--|—|–|-)\s*(.+)$", re.I)
# FARSite variant: some supplements (NRCAR) write subpart heads with NO separator at
# all -- 'Subpart 2001.1 Purpose, Authority, Issuance'. Only consulted in relaxed
# (farsite) mode, and only on text that already passed the bold-heading test; the
# title-start guard ([^a-z]) keeps prose like 'subpart 2001.1 applies ...' out.
LG_SUBPART_LOOSE = re.compile(r"^\s*SUBPART\s+(\d+\.\d+)\s+(?=[^a-z])(.+)$", re.I)
LG_PART = re.compile(r"^\s*PART\s+(\d+)\s*(?:--|—|–|-)\s*(.+)$", re.I)
# Leading junk before a heading's section number: whitespace, the CFR section sign
# (NRCAR: '§2001.101 Purpose.'), or U+FFFD -- the § of a windows-1252 file decoded as
# utf-8/replace (the 1999 NRCAR mirrors). Stripped before the section/subpart/part
# grammars run; FAR/AFARS headings never start with these, so they are unaffected.
FS_LEAD = re.compile(r"^[\s§�]+")
_ROMAN_CHARS = set("ivxlcdm")
# self-close UNCLOSED <a name> anchors (the legacy disease); the lookahead leaves
# an already-closed empty anchor alone instead of doubling its </a>
LG_ANCHOR = re.compile(r"(?i)<a\s+(name\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))\s*>(?!\s*</a>)")


def _lg_parse(path):
    """Parse a legacy file with its <A NAME> anchors SELF-CLOSED first. The era never
    closes them, so lxml nests each inside the previous until libxml2's depth cap (255)
    is hit and the rest of the file is silently DROPPED (part 32 of FAC 1997-27 loses
    everything past 32.900 otherwise)."""
    raw = open(path, encoding="utf-8", errors="replace").read()
    raw = LG_ANCHOR.sub(r"<a \1></a>", raw)
    return lxml.html.fromstring(raw)


def _lg_first_bold(el):
    """First bold descendant of el (<b> or its semantic twin <strong> -- the SOFARS
    Drupal render bolds subpart heads with <strong>) that is not swallowed inside an
    unclosed <a name> anchor (those hold the REST OF THE FILE, not heading text)."""
    def _anc_within(d):                         # ancestors of d BELOW el only
        for x in d.iterancestors():
            if x is el:
                return
            yield x
    return next((d for d in el.iter("b", "strong")
                 if not any(x.tag == "a" and x.get("name") for x in _anc_within(d))),
                None)


# a heading-shaped line 'NNNN.NNN Title' whose title starts uppercase and is short --
# used for anchor-marked headings that carry NO bold/strong at all (SOFARS Drupal
# '<p><span class="anchor"></span>5601.101 Purpose.</p>'). The uppercase-start + length
# guard keeps mid-sentence citations ('5601.101 requires ...') out.
_LG_ANCHOR_HEAD = re.compile(r"^(\d{3,4}\.\d[\d.-]*)\s+([A-Z(].{0,120})$")


def _lg_anchor_heading(el):
    """(num, title) if el is a plain <p>/<div> section heading marked only by a leading
    empty bookmark anchor -- '<span id="BM101" class="anchor"></span>5601.101 Purpose.'
    -- with no bold. Guarded to heading-shaped, short, uppercase-title lines. '' else."""
    if el.tag not in ("p", "div"):
        return None
    kids = [c for c in el if isinstance(c.tag, str)]
    if not kids:
        return None
    first = kids[0]
    is_anchor = (first.tag == "span" and "anchor" in (first.get("class") or "")) \
        or (first.tag == "a" and (first.get("name") or first.get("id")))
    if not is_anchor or (first.text or "").strip():
        return None
    if _lg_first_bold(el) is not None:          # bold heading: the other rules own it
        return None
    ft = FS_LEAD.sub("", _ww_flatten_p(el))
    m = _LG_ANCHOR_HEAD.match(ft)
    if not m or not NUMERIC.match(m.group(1)):
        return None
    title = m.group(2).strip().rstrip(".")
    return m.group(1), title


def _lg_heading_text(el):
    """The text of a potential legacy heading: any <h1>-<h5>, or a <p>/<a> whose only
    element child is a single <b> with no surrounding text. '' otherwise."""
    if el.tag in ("h1", "h2", "h3", "h4", "h5"):
        return X.norm(el.text_content())
    if el.tag in ("p", "div"):
        # A 1990-variant heading is a <p> (or a <DIV CLASS=9SecHdg>-style wrapper, the
        # BeyondPress part-1 layout) whose VISIBLE content is one bold run --
        # possibly wrapped in <font>, possibly with the rest of the file swallowed
        # inside a following unclosed <a name> (which _ww_flatten_p skips), possibly
        # with a stray trailing '.' outside the </B>. So: the p flattens to exactly
        # its first bold descendant's text.
        b = _lg_first_bold(el)
        if b is not None:
            bt = X.norm(b.text_content())
            ft = _ww_flatten_p(el).rstrip(".")
            if bt and ft == bt.rstrip(".") and len(bt) < 250:   # clause titles run long
                return bt
    return ""


def _lg_split_heading(el):
    """PARTIALLY-BOLD farsite headings (relaxed mode only). Several FARSite mirrors
    (NFS, TRANSFARS, DAFFARS ...) never close the heading <p>, so the section's body
    text is swallowed into the heading element after the </b>:

        <p><a name="P4_107"></a><b>5501.101 Purpose. </b>
        <br>The United States Transportation Command ...</p (implied)>

    The strict rule (flatten == bold run) then fails and the WHOLE section vanishes
    into audit residue. Here: if the element's first bold run is ITSELF a complete
    'NNNN.NNN Title' heading (title starting with a non-lowercase char -- prose like
    '<b>2001.105 applies ...</b>' stays body) and the visible flatten merely CONTINUES
    past it, return (num, title, bold_el) so the caller can open the section and keep
    the remainder as its first body block. Returns None otherwise."""
    if el.tag not in ("p", "div"):
        return None
    b = _lg_first_bold(el)
    if b is None:
        return None
    bt = X.norm(b.text_content())
    if not bt or len(bt) >= 250:
        return None
    ft = _ww_flatten_p(el)
    if ft == bt or not ft.startswith(bt):       # fully bold (strict rule's job) / not a prefix
        return None
    m = WW_SECTION_NUM.match(FS_LEAD.sub("", bt))
    if not m or not NUMERIC.match(m.group(1)):
        return None
    title = m.group(2).strip()
    if not title or title[:1].islower():        # heading titles never start lowercase
        return None
    return m.group(1), title, b


def _lg_blank_heading_bold(b):
    """Erase a heading bold run in-place (after _lg_split_heading matched) so the
    element re-flattens to just the swallowed body remainder. b's own tail is kept --
    it is (part of) that remainder."""
    for d in b.iter():
        d.text = None
        if d is not b:
            d.tail = None


def _lg_sections(body, relaxed=False):
    """Split a legacy part/clause-group file into sections. Returns (sections,
    {subpart_num: title}) -- content before the first section heading (the TOC tables and
    part title) is dropped.

    relaxed=True (the FARSite chunker) additionally accepts the two FARSite heading
    deviations: a §/mojibake prefix before the section number (NRCAR), handled for all
    callers by the FS_LEAD strip below, plus PARTIALLY-BOLD headings whose unclosed <p>
    swallowed the section body (_lg_split_heading) and dash-less subpart heads
    (LG_SUBPART_LOOSE). FAR legacy / AFARS transit stay on the strict rule."""
    sections, cur, subparts = [], None, {}
    for el in body.iter():
        if not isinstance(el.tag, str):
            continue
        ht = _lg_heading_text(el)
        if ht:
            ht = FS_LEAD.sub("", ht)
            m = WW_SECTION_NUM.match(ht)
            if m and NUMERIC.match(m.group(1)):
                title = m.group(2).strip()
                title = title[:-1].rstrip() if title.endswith(".") else title
                cur = {"num": m.group(1), "title": title, "blocks": []}
                sections.append(cur)
                continue
            sm = LG_SUBPART.match(ht) or (LG_SUBPART_LOOSE.match(ht) if relaxed else None)
            if sm:
                t = sm.group(2).strip().rstrip(".")
                subparts[sm.group(1)] = t.title() if t.isupper() else t
                cur = None
                continue
            if LG_PART.match(ht):
                cur = None
                continue
        elif relaxed and not any(a.tag in ("table", "p", "div")
                                 for a in el.iterancestors()):
            split = _lg_split_heading(el)
            if split:
                num, title, b = split
                title = title[:-1].rstrip() if title.endswith(".") else title
                cur = {"num": num, "title": title, "blocks": []}
                sections.append(cur)
                _lg_blank_heading_bold(b)
                if _ww_flatten_p(el):        # the swallowed body: first block
                    cur["blocks"].append(el)
                continue
            anc = _lg_anchor_heading(el)     # SOFARS Drupal: anchor-marked plain heading
            if anc:
                cur = {"num": anc[0], "title": anc[1], "blocks": []}
                sections.append(cur)
                continue
        if cur is None:
            continue
        if el.tag in ("p", "div"):
            if any(a.tag in ("table", "p", "div") for a in el.iterancestors()):
                continue                     # cell content / nested: outer block renders it
            if ht and not relaxed:
                continue                     # a bold-only line: heading/furniture in legacy
            # relaxed (farsite): a bold paragraph that matched NO heading grammar is
            # CONTENT -- label-fused paragraphs ('5522.101-1(a) Contracting Officers
            # must ...'), bold 'Alternate 2 (Oct 1999)' clause openers the alternates
            # machinery must see, agencies that bold whole notes. Dropping them (the
            # legacy rule) silently loses that text.
            cur["blocks"].append(el)
        elif el.tag == "table":
            cur["blocks"].append(el)
    return sections, subparts


def _lg_is_row_label(label, prev_letter):
    """Ladder fallback for units the store doesn't know: is this label a depth-1 row?
    Lowercase single/double letters ((a)..(z),(aa)) are depth 1 in the FAR -- except
    roman-lookalikes, which are rows only when they continue the letter ladder."""
    if not label or not label.islower() or not label.isalpha() or len(label) > 2:
        return False
    if all(c in _ROMAN_CHARS for c in label):
        return prev_letter != "" and len(label) == 1 \
            and ord(label) == ord(prev_letter) + 1
    return len(label) == 1 or label in ("aa", "bb", "cc")   # (aa) after (z)


def _lg_group(blocks, row_labels=None):
    """Group flat legacy <P>s into lines: hint-driven for known units (same contract as
    _ww_group/_w1_group); the FAR letter-ladder heuristic for unknown ones."""
    known = row_labels is not None
    remaining = dict(row_labels) if known else {}
    groups, open_g, prev_letter = [], None, ""
    for el in blocks:
        if el.tag == "table":
            open_g = None
            groups.append({"label": "", "is_row": False, "blocks": [el]})
            continue
        t = _ww_flatten_p(el)
        if not t:
            continue
        label = _ww_label(t)
        if known:
            is_row = bool(label) and remaining.get(label, 0) > 0
        else:
            is_row = _lg_is_row_label(label, prev_letter)
        if is_row:
            if known:
                remaining[label] -= 1
            else:
                prev_letter = label
            open_g = {"label": label, "is_row": True, "blocks": [el]}
            groups.append(open_g)
        elif open_g is not None:
            open_g["blocks"].append(el)
        else:
            open_g = {"label": "", "is_row": False, "blocks": [el]}
            groups.append(open_g)
    return groups


def _lg_find_end_and_alt(blocks):
    """(end_marker, end_index, alt_start_index): the '(End of clause/provision)' text
    paragraph, and the first following 'Alternate <roman>' opener."""
    end_text, end_idx = "", -1
    for i, el in enumerate(blocks):
        em = _end_marker(_ww_flatten_p(el), _cls(el))
        if em:
            end_text, end_idx = em, i; break
    alt_idx = -1
    for i in range(end_idx + 1 if end_idx >= 0 else 0, len(blocks)):
        if blocks[i].tag == "p" and CK.ALT_OPENER.match(_ww_flatten_p(blocks[i])):
            alt_idx = i; break
    return end_text, end_idx, alt_idx


def _lg_meta(blocks, end_text):
    """(instrument, date, prescribed_by): instrument from the End marker / prefatory line;
    date from the centered dated-title <P> '... (Mon YYYY)'; prescribed_by as usual."""
    instrument = ("clause" if end_text == "(End of clause)"
                  else "provision" if end_text == "(End of provision)" else "")
    if not instrument:
        for el in blocks[:3]:
            low = _ww_flatten_p(el).lower()
            if "following clause" in low:
                instrument = "clause"; break
            if "following provision" in low:
                instrument = "provision"; break
    date, prescribed_by = "", ""
    if instrument:
        for el in blocks[:5]:
            t = _ww_flatten_p(el)
            if not prescribed_by:
                m = CK.PRESCRIBED.search(t)
                if m:
                    prescribed_by = m.group(1).replace(" ", "")
            if not date and (el.get("align") or "").lower() == "center":
                dm = CK.ALT_DATE.search(t)
                if dm:
                    date = X.norm(f"{dm.group(1)} {dm.group(2)}")
    return instrument, date, prescribed_by


def _lg_build_unit(sec, cfg, row_labels=None):
    """Build the chunk rows for one legacy section."""
    num, title, blocks = sec["num"], sec["title"], sec["blocks"]
    reg = cfg["regulation"]
    url = cfg["url_template"].format(num=num)
    base_type = "subsection" if "-" in num else "section"
    end_text, end_idx, alt_idx = _lg_find_end_and_alt(blocks)
    instrument, bdate, bpresc = _lg_meta(blocks, end_text)
    cut = alt_idx if alt_idx >= 0 else len(blocks)
    base_blocks = [b for i, b in enumerate(blocks) if i < cut and i != end_idx]

    def row(number, typ, tokens, ps, text, scan_blocks, alternate="",
            date="", prescribed_by="", end_marker=""):
        r = {"citation": f"{reg}-{number}", "regulation": reg,
             "source_version": cfg.get("source_version", ""),
             "pipeline_version": cfg.get("pipeline_version", ""),
             "type": typ, "instrument": instrument, "alternate": alternate,
             "part_title": "", "subpart_title": "",
             "section_title": "", "subsection_title": ""}
        r.update(CK.decompose(num, tokens, ["paragraph"][:cfg["bottom_depth"]]))
        r["url"] = url
        r["cross_references"] = _ww_collect_refs(ps, num)
        r["external_references"] = _ww_collect_external(ps)
        imgs = []
        for el in scan_blocks:
            for im in ([el] if el.tag == "img" else el.findall(".//img")):
                iid = X.img_id(im.get("src") or "")
                if iid not in imgs:
                    imgs.append(iid)
        r["images"] = imgs
        r["changes"] = []
        r["date"] = date
        r["prescribed_by"] = prescribed_by
        r["reserved"] = X.norm(text).rstrip(".").lower().endswith("[reserved]")
        r["end_marker"] = end_marker
        r["text"] = text
        return r

    groups = _lg_group(base_blocks, row_labels)
    unit_text = "\n".join(t for t in (_ww_group_text(g) for g in groups) if t)
    unit_ps = [el for g in groups for el in g["blocks"]]
    rows = [row(num, base_type, [], unit_ps, unit_text, base_blocks,
                date=bdate, prescribed_by=bpresc, end_marker=end_text)]

    if cfg["bottom_depth"] >= 1:
        for g in groups:
            if g["is_row"]:
                rows.append(row(f"{num}({g['label']})", CK.level_name(1), [g["label"]],
                                g["blocks"], _ww_group_text(g), g["blocks"]))

    if alt_idx >= 0:
        spans, curspan = [], None
        for el in blocks[alt_idx:]:
            m = CK.ALT_OPENER.match(_ww_flatten_p(el)) if el.tag == "p" else None
            if m:
                curspan = {"roman": m.group(1).upper(), "blocks": [el]}
                spans.append(curspan)
            elif curspan is not None:
                curspan["blocks"].append(el)
        for sp in spans:
            atext = " ".join(s for el in sp["blocks"] for s in _ww_flatten_block(el))
            lead = _ww_flatten_p(sp["blocks"][0]).split(".", 1)[0]
            adm = CK.ALT_DATE.search(lead)
            apm = CK.PRESCRIBED.search(_ww_flatten_p(sp["blocks"][0]))
            rows.append(row(num, base_type, [], sp["blocks"], atext, sp["blocks"],
                            alternate=CK.roman_to_arabic(sp["roman"]),
                            date=X.norm(f"{adm.group(1)} {adm.group(2)}") if adm else "",
                            prescribed_by=apm.group(1).replace(" ", "") if apm else ""))

    own = "subsection_title" if "-" in num else "section_title"
    for r in rows:
        r[own] = title
    if len(rows) == 1 and len(unit_text) < cfg.get("min_text", MIN_TEXT):
        return []
    return rows


def _lg_files(root):
    """Legacy content files in FAR order: NN.html / N.html / NNPART.HTM part files plus
    52_NNN.html clause groups; TOC/matrix/notes excluded."""
    out = []
    for f in sorted(glob.glob(os.path.join(root, "*"))):
        b = os.path.basename(f)
        if re.match(r"^\d{1,2}(PART)?\.html?$", b, re.I) \
                or re.match(r"^52[_-]\d+\.html?$", b, re.I):
            out.append(f)
    def key(f):
        b = os.path.basename(f)
        m = re.match(r"^(\d+)(?:PART)?(?:[_-](\d+))?", b, re.I)
        return (int(m.group(1)), int(m.group(2) or 0)) if m else (999, 0)
    return sorted(out, key=key)


def chunk_edition_legacy(edition_dir, cfg, hints=None):
    """All chunk rows for one legacy-era archive edition folder."""
    hints = hints or {}
    root = lg_root(edition_dir)
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    row_labels_map = _ww_row_labels(hints)
    known_units = set(hints.get("units") or {})
    rows, manifest = [], {"processed": [], "skipped": []}
    for f in _lg_files(root):
        try:
            doc = _lg_parse(f)
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f), "reason": repr(e)[:60]})
            continue
        body = doc.find(".//body")
        if body is None:
            body = doc
        sections, subparts = _lg_sections(body)
        for k, v in subparts.items():
            subpart_titles.setdefault(k, v)
        for sec in sections:
            rl = (row_labels_map.get(sec["num"], {}) if sec["num"] in known_units else None)
            try:
                r = _lg_build_unit(sec, cfg, rl)
            except Exception as e:
                manifest["skipped"].append({"file": sec["num"], "reason": repr(e)[:70]})
                continue
            if r:
                rows.extend(r)
                manifest["processed"].append(sec["num"])
            else:
                manifest["skipped"].append({"file": sec["num"], "reason": "near-empty"})
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


def lg_root(edition_dir):
    """The folder holding the legacy part files (usually <dir>/html)."""
    if glob.glob(os.path.join(edition_dir, "[0-9]*.htm*")):
        return edition_dir
    hits = glob.glob(os.path.join(edition_dir, "**", "0[0-9].html"), recursive=True) \
        or glob.glob(os.path.join(edition_dir, "**", "[0-9]*.htm*"), recursive=True)
    return os.path.dirname(hits[0]) if hits else os.path.join(edition_dir, "html")


# ================================================================ TRANSIT ERA (AFARS 1996-2018)
# "HTML Transit" generator output (v4.0 1996 -> v9.0 2018): one file per part
# (AFAR1.htm early, 5101.htm later), a linked TOC (ul/li #hrefs -- dropped: no section is
# open yet), then flat <p> bodies with LEGACY-style headings: a <p> flattening to its
# bold run ('<b>5101.101 -- Purpose.</b>', sometimes font-wrapped). Reuses the legacy
# machinery wholesale; only the file lister and the '--' title separator differ. The
# earliest editions (96-x) use pre-renumbering part numbers (1.xxx) -- their sections
# close when the store reaches the 5101 renumbering, which is historically accurate.

def _tr_files(root):
    out = []
    for f in sorted(glob.glob(os.path.join(root, "*.htm*"))):
        b = os.path.basename(f)
        if re.match(r"(?i)^(AFAR\d+|51\d\d)\.html?$", b):
            out.append(f)
    def key(f):
        m = re.search(r"(\d+)", os.path.basename(f))
        return int(m.group(1)) if m else 999
    return sorted(out, key=key)


def tr_root(edition_dir):
    hits = [f for f in glob.glob(os.path.join(edition_dir, "**", "*.htm*"), recursive=True)
            if re.match(r"(?i)^(AFAR\d+|51\d\d)\.html?$", os.path.basename(f))
            and "__MACOSX" not in f]
    return os.path.dirname(sorted(hits)[0]) if hits else edition_dir


def chunk_edition_transit(edition_dir, cfg, hints=None):
    """All chunk rows for one HTML-Transit-era AFARS edition folder."""
    hints = hints or {}
    root = tr_root(edition_dir)
    if not _tr_files(root):
        # Non-AFARS supplements also ship HTML-Transit renders, but with per-part
        # names _tr_files doesn't know (VAAR '801.htm', DFARS '201.htm', NMCARS ...)
        # and FARSite-style page structure. The farsite chunker IS the generic
        # HTML-Transit parser (any-name lister + relaxed heading rules), so delegate
        # instead of producing the classic silent 0 rows.
        return chunk_edition_farsite(edition_dir, cfg, hints)
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    row_labels_map = _ww_row_labels(hints)
    known_units = set(hints.get("units") or {})
    rows, manifest = [], {"processed": [], "skipped": []}
    for f in _tr_files(root):
        try:
            doc = _lg_parse(f)
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f), "reason": repr(e)[:60]})
            continue
        body = doc.find(".//body")
        if body is None:
            body = doc
        sections, subparts = _lg_sections(body)
        for k, v in subparts.items():
            subpart_titles.setdefault(k, v)
        for sec in sections:
            sec["title"] = re.sub(r"^[-–—]+\s*", "", sec["title"])   # '5101.101 -- Purpose'
            rl = (row_labels_map.get(sec["num"], {}) if sec["num"] in known_units else None)
            try:
                r = _lg_build_unit(sec, cfg, rl)
            except Exception as e:
                manifest["skipped"].append({"file": sec["num"], "reason": repr(e)[:70]})
                continue
            if r:
                rows.extend(r)
                manifest["processed"].append(sec["num"])
            else:
                manifest["skipped"].append({"file": sec["num"], "reason": "near-empty"})
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


_FS_SKIP_RX = re.compile(r"(?i)toc|index|search|foreword|cover|contents|abbrev|whatsnew"
                         r"|^(mp|ig)[\d_]|^pgi[\d_.-]|attachment|^\d{2}-\d|^annex")
# the last five: companion docs (DAFFARS MP/IG mandatory procedures + PGI pages --
# both 'MP5301.601' and 'mp_5301.601'/'pgi_5304' namings, SOFARS form attachments /
# 'NN-M<Form>.htm' pages, NMCARS J&A annex templates) -- a different document class,
# never section rows; a bold 'NUM Title' inside one would fabricate polluting sections

# FARSite raw-HTML repairs (generator artifacts the tree-level rules can't reach):
# 1) headings SPLIT ACROSS word-by-word bold runs -- '<b>1808.103  Information</b>
#    <b>on</b> <b>available</b> ...' -- merged back into one run;
# 2) headings that open at a bare '<br>' instead of their own <p> (NFS writes whole
#    section sequences as one flow separated by <br>): promoted to a fresh <p> so the
#    section machinery sees a block boundary. Only bolds that LOOK like a section
#    number (or SUBPART) are promoted; a mid-sentence bold citation has '(' after the
#    number and stays put.
_FS_BOLD_MERGE_RX = re.compile(r"(?i)</b>(\s|&nbsp;)*<b>")
_FS_BR_HEAD_RX = re.compile(
    r"(?i)<br\s*/?>\s*((?:<a\s+name[^>]*>|</a>|<b>\s*</b>|\s)*)"
    r"(<b>\s*(?:[§�]?\d{1,4}\.\d+(?:-\d+)*|SUBPART)\s)")


def _fs_parse(path):
    """_lg_parse plus the FARSite raw repairs above."""
    raw = open(path, encoding="utf-8", errors="replace").read()
    raw = LG_ANCHOR.sub(r"<a \1></a>", raw)
    raw = _FS_BOLD_MERGE_RX.sub(" ", raw)
    raw = _FS_BR_HEAD_RX.sub(r"</p><p>\1\2", raw)
    return lxml.html.fromstring(raw)


def _fs_root(edition_dir):
    """Deepest dir holding the most content pages. Naming varies per agency:
    DOLAR '2902.htm', DFARS 'Dfar201.htm', appendices 'DFARSApxA.htm' -- so take
    every html page except obvious furniture and let the section detector decide
    (pages with no 'NNNN.NNN Title' heads simply yield nothing)."""
    best, bestn = "", 0
    for r, _, fs in os.walk(edition_dir):
        n = sum(1 for f in fs if f.lower().endswith((".htm", ".html"))
                and not _FS_SKIP_RX.search(f))
        if n > bestn:
            best, bestn = r, n
    return best or edition_dir


def _fs_files(root):
    return sorted(os.path.join(root, f) for f in os.listdir(root)
                  if f.lower().endswith((".htm", ".html")) and not _FS_SKIP_RX.search(f))


def chunk_edition_farsite(edition_dir, cfg, hints=None):
    """FARSite mirrors (farsite.hill.af.mil; HTML Transit render, 1998-2018): one page
    per part, bold-run 'NNNN.NNN Title.' section heads and 'Subpart NNNN.N Title'
    breaks -- the legacy machinery applies once the JS date-stamp scripts, TRANSIT
    nav comments, and gif-button furniture are stripped."""
    hints = hints or {}
    cfg = dict(cfg)
    # supplements are full of legitimate one-sentence sections ('See FAR ...',
    # '[Reserved]'); the FAR-tuned near-empty bar would silently drop them
    cfg.setdefault("min_text", 12)
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    row_labels_map = _ww_row_labels(hints)
    known_units = set(hints.get("units") or {})
    root = _fs_root(edition_dir)
    rows, manifest = [], {"processed": [], "skipped": []}
    for f in _fs_files(root):
        try:
            doc = _fs_parse(f)
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f), "reason": repr(e)[:60]})
            continue
        body = doc.find(".//body")
        if body is None:
            body = doc
        for bad in body.xpath(".//script | .//comment()"):
            p = bad.getparent()
            if p is not None:
                p.remove(bad)
        sections, subparts = _lg_sections(body, relaxed=True)
        for k, v in subparts.items():
            subpart_titles.setdefault(k, v)
        for el in body.iter("p"):                      # 'DOLAR Part 2901 <title>' head
            t = " ".join(el.text_content().split())
            m = re.match(r"(?i)^(?:[A-Z]{2,8}\s+)?PART\s+(\d{3,4})\s*[-–—:]?\s*(.{3,140})$", t)
            if m:
                part_titles.setdefault(m.group(1), m.group(2).strip())
                break
        for sec in sections:
            sec["title"] = re.sub(r"^[-–—]+\s*", "", sec["title"])
            rl = (row_labels_map.get(sec["num"], {}) if sec["num"] in known_units else None)
            try:
                r = _lg_build_unit(sec, cfg, rl)
            except Exception as e:
                manifest["skipped"].append({"file": sec["num"], "reason": repr(e)[:70]})
                continue
            if r:
                rows.extend(r)
                manifest["processed"].append(sec["num"])
            else:
                manifest["skipped"].append({"file": sec["num"], "reason": "near-empty"})
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ================================================================ AGOV ERA (AFARS 2021-2022)
# Drupal-site render of a Word export: <h2 class="Heading2"> subparts,
# <h3/h4 class="Heading3/4"> sections/subsections (each stuffed with _Toc anchors),
# plain <p> paragraphs (labels literal, no depth markers -- legacy grouping applies),
# a TOC of <p class="TOC1..3"> lines and a 'regnavigation' block to drop.

def _ag_sections(body):
    sections, cur, subparts = [], None, {}
    for el in body.iter():
        if not isinstance(el.tag, str):
            continue
        cls = el.get("class") or ""
        if el.tag in ("h1", "h2", "h3", "h4", "h5"):
            t = X.norm(el.text_content())
            m = WW_SECTION_NUM.match(t)
            if m and NUMERIC.match(m.group(1)):
                title = m.group(2).strip()
                title = title[:-1].rstrip() if title.endswith(".") else title
                cur = {"num": m.group(1), "title": title, "blocks": []}
                sections.append(cur)
            else:
                sm = WW_SUBPART.search(t)
                if sm:
                    subparts[sm.group(1)] = sm.group(2).rstrip(".").strip()
                cur = None
            continue
        if cur is None:
            continue
        if el.tag == "p":
            if cls.startswith("TOC") or "regnavigation" in cls:
                continue
            if any(a.tag in ("table",) for a in el.iterancestors()):
                continue
            cur["blocks"].append(el)
        elif el.tag == "table":
            cur["blocks"].append(el)
    return sections, subparts


def chunk_edition_agov(edition_dir, cfg, hints=None):
    """All chunk rows for one agov-era (Drupal/Word-export) AFARS edition folder."""
    hints = hints or {}
    part_rx = re.compile(r"AFARS[-_](Part|PART)[-_]\d+\.html$")
    files = sorted(f for f in glob.glob(os.path.join(edition_dir, "**", "*.html"),
                                        recursive=True)
                   if part_rx.match(os.path.basename(f)) and "__MACOSX" not in f)
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    row_labels_map = _ww_row_labels(hints)
    known_units = set(hints.get("units") or {})
    rows, manifest = [], {"processed": [], "skipped": []}
    for f in files:
        try:
            doc = lxml.html.parse(f).getroot()
        except Exception as e:
            manifest["skipped"].append({"file": os.path.basename(f), "reason": repr(e)[:60]})
            continue
        body = doc.find(".//body")
        if body is None:
            continue
        sections, subparts = _ag_sections(body)
        for k, v in subparts.items():
            subpart_titles.setdefault(k, v)
        for sec in sections:
            rl = (row_labels_map.get(sec["num"], {}) if sec["num"] in known_units else None)
            try:
                r = _lg_build_unit(sec, cfg, rl)
            except Exception as e:
                manifest["skipped"].append({"file": sec["num"], "reason": repr(e)[:70]})
                continue
            if r:
                rows.extend(r)
                manifest["processed"].append(sec["num"])
            else:
                manifest["skipped"].append({"file": sec["num"], "reason": "near-empty"})
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ================================================================ DITA-ZIP ERA (AFARS 2022+)
# Editions that ship the actual DITA source (`*_DITA_Files.zip` with a ditamap and
# per-section .dita files -- the same acquisition.gov toolchain as the FAR GitHub repo).
# No HTML adapter needed: extract and run the REAL chunker.py, which yields canon rows
# (full <xref> refs, exact flattening) rather than a reconstruction from rendered HTML.

def chunk_edition_dita(edition_dir, cfg, hints=None):
    """Chunk an edition from its bundled DITA source zip via chunker.run_chunker."""
    import tempfile, shutil, zipfile
    zips = glob.glob(os.path.join(edition_dir, "**", "*DITA*Files*.zip"), recursive=True) \
        or glob.glob(os.path.join(edition_dir, "*.zip"))
    if not zips:
        raise SystemExit(f"no DITA zip in {edition_dir}")
    tmp = tempfile.mkdtemp(prefix="dita_zip_")
    try:
        with zipfile.ZipFile(zips[0]) as z:
            z.extractall(tmp)
        mapname = cfg.get("ditamap") or "FAR.ditamap"
        maps = glob.glob(os.path.join(tmp, "**", mapname), recursive=True)
        input_dir = os.path.dirname(maps[0]) if maps else tmp
        c = dict(cfg)
        c["input_dir"] = input_dir
        c.pop("files", None)
        rows, manifest, _ = CK.run_chunker(c)
        # AFARS part-title quirks: files are 'PART_5101.dita' (uppercase -- the chunker's
        # case-sensitive 'Part_*.dita' harvest misses them on Linux), and some zips embed
        # an 'AFARS – PART 5101 ' prefix in the title text. Fill the gap + normalize.
        if rows and not any(r.get("part_title") for r in rows):
            part_titles = {}
            for p in glob.glob(os.path.join(input_dir, "*.dita")):
                m = re.match(r"(?i)part[-_](\d+)\.dita$", os.path.basename(p))
                if m:
                    part_titles[m.group(1)] = CK._file_title(p)
            for r in rows:
                if not r.get("part_title"):
                    r["part_title"] = part_titles.get(r["part"], "")
        pfx = re.compile(r"(?i)^AFARS\s*[-–—]?\s*PART\s+\d+\s*[-–—]?\s*")
        for r in rows:
            if r.get("part_title"):
                r["part_title"] = pfx.sub("", r["part_title"]).strip()
        return rows, manifest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- eCFR DITA (GitHub canon)
# The 22 agency GitHub repos that ship raw eCFR DITA: one topic file per part/subpart/
# section (Section_422_302_T48_….dita), a chapter-named ditamap, outputclass="ecfr".
# The section files are the SAME DITA the real chunker.build() already handles (title
# <ph props="autonumber">NUM</ph>, conbody flat <p outputclass="ListN"> with literal
# labels) -- only the FILE NAMING differs from FAR (run_chunker's numeric-filename filter
# is why it returned 0 rows). So this adapter reuses build() verbatim for sections and
# only adds eCFR file discovery + Part/Subpart title parsing.
_ECFR_TITLE_PREFIX = re.compile(r"(?i)^(?:PART|SUBPART|CHAPTER|SUBCHAPTER)\s+[\d.]+\s*[—–-]*\s*")


def _canon_title(raw):
    """Clean a Part/Subpart container title for the breadcrumb: strip the leading
    'PART 5806 - ' label (repeated when the source doubles it, e.g. 'PART 5806PART
    5806 - COMPETITION REQUIREMENTS'), then title-case an ALL-CAPS heading so it matches
    the archive eras' casing ('COMPETITION REQUIREMENTS' -> 'Competition Requirements')."""
    t, prev = (raw or "").strip(), None
    while t != prev:
        prev = t
        t = _ECFR_TITLE_PREFIX.sub("", t).strip()
    if t and t.isupper():
        t = t.title()
    return t


def _ecfr_dita_dir(edition_dir):
    """The dir holding the eCFR topic files + ditamap (repo root or its dita/)."""
    for cand in (os.path.join(edition_dir, "dita"), edition_dir):
        if glob.glob(os.path.join(cand, "Section_*.dita")) \
                or glob.glob(os.path.join(cand, "*.ditamap")):
            return cand
    hits = glob.glob(os.path.join(edition_dir, "**", "Section_*.dita"), recursive=True)
    return os.path.dirname(hits[0]) if hits else os.path.join(edition_dir, "dita")


def _ecfr_canonical_map(dita_dir, ditamap=None):
    """Pick the one authoritative ditamap. A forced name wins (e.g. 'PGI.ditamap' to pull
    DFARSPGI out of the DFARS repo). Otherwise drop the repos' housekeeping maps
    (*_backup, *FixDitamapAttributes, *-ONLY, nodoctype, editables, fillins, alt*) and
    prefer the eCFR 'Chapter_*_T48' map, else the shortest remaining name (the plain
    '<REG>.ditamap' over combined/appendix variants)."""
    if ditamap:
        hit = glob.glob(os.path.join(dita_dir, ditamap)) \
            or glob.glob(os.path.join(dita_dir, "**", ditamap), recursive=True)
        if hit:
            return hit[0]
    cand = [m for m in glob.glob(os.path.join(dita_dir, "*.ditamap"))
            if not re.search(r"(?i)backup|fixditamap|nodoctype|editables|fillins|alt\d|-only|combined",
                             os.path.basename(m))]
    if not cand:
        return None
    return sorted(cand, key=lambda m: (0 if re.search(r"(?i)chapter", os.path.basename(m))
                                       else 1, len(os.path.basename(m)), m))[0]


def _ecfr_files(dita_dir, ditamap=None):
    """Content topic files, ordered by the canonical ditamap when present, else globbed."""
    m = _ecfr_canonical_map(dita_dir, ditamap)
    if m:
        try:
            _, hrefs = CK.parse_ditamap(m)
            files = [os.path.join(dita_dir, h) for h in hrefs
                     if os.path.isfile(os.path.join(dita_dir, h))]
            if files:
                return files
        except Exception:
            pass
    return sorted(glob.glob(os.path.join(dita_dir, "*.dita")))


def chunk_edition_canon(edition_dir, cfg, hints=None):
    """All chunk rows for one CANONICAL DITA edition (a GitHub repo checkout or an
    acquisition.gov DITA zip export). Handles BOTH source shapes with one path, because
    the real chunker.build() derives the section number from the title <ph autonumber>,
    not the filename:
      * eCFR part-per-topic repos (Section_422_302_T48_….dita, Chapter_*_T48 ditamap) --
        22 agencies;
      * numeric FAR-style repos (5101.101.dita / 5333_105.dita, <REG>.ditamap) -- AFARS,
        GSAM, DFARS, NMCARS, DAFFARS, DLAD, SOFARS, DARS.
    build() reuse means all DITA flatten/label/table/xref/alternate/clause logic is
    shared; this function only adds file discovery, container-title parsing, and the
    NUMERIC-citation filter that drops TOC/container topics. cfg['ditamap'] selects the
    map when a repo ships several (e.g. DFARS 'PGI.ditamap' for the DFARSPGI store)."""
    hints = hints or {}
    dita_dir = _ecfr_dita_dir(edition_dir)
    cfg = dict(cfg)
    cfg["input_dir"] = dita_dir
    cfg.setdefault("min_text", 12)         # keep terse-but-real supplement sections
    part_titles = dict(hints.get("part_titles", {}))
    subpart_titles = dict(hints.get("subpart_titles", {}))
    rows, manifest = [], {"processed": [], "skipped": []}
    files = list(_ecfr_files(dita_dir, cfg.get("ditamap")))
    X.set_id_map(CK.build_id_map(files))       # resolve opaque xref ids ('#SSQFWIXN') to section citations
    for p in files:
        b = os.path.basename(p)
        # part/subpart CONTAINER files (both eCFR 'Part_422_T48'/'Subpart_422_3_T48' and
        # FAR-style 'Part_5'/'Subpart_5.1') carry only a title -- harvest it, no row
        mp = re.match(r"(?i)Part[_-](\d+)(?:[_.]|$)", b)
        ms = re.match(r"(?i)Subpart[_-](\d+)[_.](\w+?)(?:_T48|\.dita$)", b)
        if mp and not re.match(r"(?i)Section", b):
            t = _canon_title(CK._file_title(p))
            if t:
                part_titles.setdefault(mp.group(1), t)
            continue
        if ms:
            t = _canon_title(CK._file_title(p))
            if t:
                subpart_titles.setdefault(f"{ms.group(1)}.{ms.group(2)}", t)
            continue
        if re.match(r"(?i)(Chapter|Subchapter)[_-]", b):
            continue                           # TOC-only containers: no rows
        far = os.path.splitext(b)[0]
        try:
            r, reason = CK.build(p, far, cfg)
        except Exception as e:
            manifest["skipped"].append({"file": b, "reason": repr(e)[:70]})
            continue
        num = r[0]["citation"].split("-", 1)[-1].split("(")[0] if r else ""
        if r and NUMERIC.match(num):
            rows.extend(r)
            manifest["processed"].append(num)
        else:
            if r and cfg.get("capture_companions"):
                dc = classify_companion(b, r[0].get("section_title", ""),
                                        r[0].get("citation", ""))
                if dc:
                    manifest.setdefault("companions", []).append(
                        {"doc_class": dc, "file": b, "rows": r})
            manifest["skipped"].append({"file": b, "reason": reason or "non-section"})
    X.set_id_map({})                           # clear the per-edition id map
    rows.sort(key=CK.sort_key)
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:
            r["section_title"] = section_titles.get(sec_of(r), "")
    manifest["rows"] = len(rows)
    return rows, manifest


# ---------------------------------------------------------------- edition metadata
def ww_root(edition_dir):
    """The folder holding the webworks Subpart_*.html files (usually <dir>/html)."""
    if glob.glob(os.path.join(edition_dir, "Subpart*_*.html")):
        return edition_dir
    hits = glob.glob(os.path.join(edition_dir, "**", "Subpart*5_1.html"), recursive=True) \
        or glob.glob(os.path.join(edition_dir, "**", "Subpart*_*.html"), recursive=True)
    return os.path.dirname(hits[0]) if hits else os.path.join(edition_dir, "html")


def edition_root(edition_dir):
    """The folder actually holding the <REG>_Part_*.html files -- some downloads nest
    them (2021-07: web/<host>/html/current/far/html_part/; AFARS: <dir>/CURRENT/)."""
    rx = re.compile(r"(FAR|AFARS)[-_](Part|PART)[-_]\d+\.html$")
    for pat in ("*.html", os.path.join("**", "*.html")):
        hits = [f for f in glob.glob(os.path.join(edition_dir, pat), recursive=True)
                if rx.match(os.path.basename(f)) and "__MACOSX" not in f]
        if hits:
            return os.path.dirname(sorted(hits)[0])
    return edition_dir


# Per-regulation profiles: the same machinery serves FAR, AFARS, ... -- what differs is
# the citation URL, the ditamap name (for editions shipping DITA source), and which
# scraped-dates file maps folder names to authoritative effective dates.
REG_PROFILES = {
    "FAR":   {"url_template": "https://www.acquisition.gov/far/{num}",
              "ditamap": "FAR.ditamap",
              "dates": os.path.join(DATA_DIR, "archive_dates.json"),
              "bottom_depth": 1},
    # AFARS DITA writes paragraph labels as literal text in top-level <p>s (no <ol>/<li>),
    # so the canon chunker produces UNIT rows only -- every era matches that (paragraphs
    # live as lines inside the unit text), which also sidesteps the letter-ladder
    # ambiguity in the label-free HTML eras.
    "AFARS": {"url_template": "https://www.acquisition.gov/afars/{num}",
              "ditamap": "AFARS.ditamap",
              "dates": os.path.join(DATA_DIR, "afars_dates.json"),
              "bottom_depth": 0},
}

AGENCIES_PATH = os.path.join(DATA_DIR, "agencies.json")


def ensure_profile(regulation, dates_path="", bottom_depth=None):
    """Register (or update) a regulation profile at runtime. Unknown agencies get a
    synthesized profile: acquisition.gov URL, <REG>.ditamap, AFARS-style unit rows
    unless agencies.json / the caller says otherwise. Returns the profile."""
    reg = regulation.upper()
    if reg not in REG_PROFILES:
        cfgd = {}
        if os.path.exists(AGENCIES_PATH):
            cfgd = (json.load(open(AGENCIES_PATH, encoding="utf-8")) or {}).get(reg, {}) or {}
        REG_PROFILES[reg] = {
            "url_template": cfgd.get("url_template",
                                     f"https://www.acquisition.gov/{reg.lower()}/{{num}}"),
            "ditamap": cfgd.get("ditamap", f"{reg}.ditamap"),
            # bare filenames in agencies.json resolve against pipeline/data/
            "dates": (os.path.join(DATA_DIR, cfgd["dates"])
                      if cfgd.get("dates") and not os.path.isabs(cfgd["dates"])
                      else cfgd.get("dates", "")),
            "bottom_depth": cfgd.get("bottom_depth", 0),
        }
    p = REG_PROFILES[reg]
    if dates_path:
        p["dates"] = dates_path
    if bottom_depth is not None:
        p["bottom_depth"] = bottom_depth
    return p


def eras_path(regulation):
    """Canonical location of the survey output (era classification) for a regulation."""
    return os.path.join(CACHE_DIR, "archive_eras.json" if regulation == "FAR"
                        else f"{regulation.lower()}_eras.json")


ARCHIVE_DATES_PATH = os.path.join(DATA_DIR, "archive_dates.json")
_ARCHIVE_DATES = {}
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]


def load_archive_dates(path=ARCHIVE_DATES_PATH):
    """Authoritative {folder_name: {number, effective_date}} scraped from
    acquisition.gov/archives (every edition's published effective date; one file per
    regulation, see REG_PROFILES). Resolves the handful of folder-name collisions by
    preferring the entry whose number appears in the folder name. Cached per path.
    Empty dict if the file is absent."""
    if path not in _ARCHIVE_DATES:
        table = {}
        if os.path.exists(path):
            for m in json.load(open(path, encoding="utf-8")):
                f = m.get("folder_name")
                if not f:
                    continue
                if f in table and m.get("number", "") not in f:
                    continue                 # keep the entry whose number matches the folder
                table[f] = m
        _ARCHIVE_DATES[path] = table
    return _ARCHIVE_DATES[path]


def _source_version(number, iso_date, label="FAC"):
    """'2005-99' + '2018-06-15' -> 'FAC 2005-99 June 15, 2018' (the ditamap-rev
    convention); AFARS editions use the 'AFARS' label."""
    try:
        y, mo, d = (int(x) for x in iso_date.split("-"))
        return f"{label} {number} {_MONTH_NAMES[mo - 1]} {d}, {y}"
    except (ValueError, IndexError):
        return f"{label} {number}"


def parse_meta(edition_dir, regulation="FAR"):
    """Edition label + effective date for one archive edition folder.

    Primary source: the regulation's scraped dates file (authoritative, covers every
    era), keyed by the folder's basename -- defaulting to the downloader's
    archive_metadata.json next to the edition folders. Falls back to the ditaot
    LSATable.html parse when the folder isn't in the table."""
    folder = os.path.basename(os.path.normpath(edition_dir))
    profile = ensure_profile(regulation)
    label = "FAC" if regulation == "FAR" else regulation
    dates = profile.get("dates", "")
    if not dates or not os.path.exists(dates):
        sib = os.path.join(os.path.dirname(os.path.normpath(edition_dir)),
                           "archive_metadata.json")
        if os.path.exists(sib):
            dates = sib
    m = load_archive_dates(dates).get(folder) if dates else None
    if m and m.get("effective_date", "") > "1980":     # skip the 2005-0 epoch placeholder
        num, date = m["number"], m["effective_date"]
        return {"fac": f"{label} {num}", "effective_date": date,
                "source_version": _source_version(num, date, label)}

    out = {"fac": "", "effective_date": "", "source_version": ""}
    lsa = os.path.join(edition_root(edition_dir), "LSATable.html")
    if os.path.exists(lsa):
        t = re.sub(r"\s+", " ", lxml.html.parse(lsa).getroot().text_content())
        mt = re.search(r"FAC\s*(\d{4}-\d+)\s*(?:Effect\w*)?\s*"
                       r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", t)
        if mt:
            fac, mon_name, day, year = mt.groups()
            mon = MONTHS.get(mon_name[:3].lower())
            if mon:
                out["fac"] = f"FAC {fac}"
                out["effective_date"] = f"{year}-{mon:02d}-{int(day):02d}"
                out["source_version"] = f"FAC {fac} {mon_name} {int(day)}, {year}"
    return out


# ---------------------------------------------------------------- hints derivation
NOSPACE_RX = re.compile(r"\(([A-Za-z0-9]{1,4})\)(?=[^\s.)(,;:—–\-\]])")


def _line_hints(text):
    """Per-line hint skeletons.  `ns` = the label joints written WITHOUT a following
    space IN THIS LINE ('(ii)Sold in su…') -- scoped per line because the same unit
    can spell the same joint both ways on different lines (52.247-51)."""
    out = []
    for line in text.split("\n"):
        if not line:
            continue
        ns = sorted({f"({m.group(1)})" + line[m.end():m.end() + 10]
                     for m in NOSPACE_RX.finditer(line)})
        h = {"t": line[:LINE_CAP], "rows": []}
        if ns:
            h["ns"] = ns
        out.append(h)
    return out


def derive_hints(store_dir, date, regulation="FAR"):
    """Per-unit line/row skeletons from the store's as_of(date) view."""
    st = Store(store_dir, regulation)
    units, part_titles, subpart_titles = {}, {}, {}
    asof = st.as_of(date)
    para_rows = {}
    for r in asof:
        if r.get("part_title"):
            part_titles.setdefault(r["part"], r["part_title"])
        if r.get("subpart_title"):
            subpart_titles.setdefault(f'{r["part"]}.{r["subpart"]}', r["subpart_title"])
        cit = r["citation"].split("-", 1)[1]
        if not r.get("alternate") and "(" in cit:
            base, label = cit.split("(", 1)
            para_rows.setdefault(base, []).append(
                {"l": label.rstrip(")"), "text": r["text"]})
    for r in asof:
        cit = r["citation"].split("-", 1)[1]
        if "(" in cit:
            continue
        if r.get("alternate"):
            hu = units.setdefault(cit, {"lines": [], "end": "", "alts": {}})
            hu["alts"][r["alternate"]] = {"lines": _line_hints(r["text"])}
            continue
        hu = units.setdefault(cit, {"lines": [], "end": "", "alts": {}})
        hu["lines"] = _line_hints(r["text"])
        hu["end"] = r.get("end_marker", "")
        # place each paragraph row on its line at its position; rows within a line are
        # sorted by DOCUMENT position (store citation order can differ: 52.209-5's
        # '(A)…(D)…(2)' region), which is the order the adapter's block scan meets them
        lines = [l for l in r["text"].split("\n") if l]
        placements = []
        for pr in para_rows.get(cit, []):
            head = pr["text"][:HEAD_CAP]
            spot = next(((j, p0) for j, line in enumerate(lines)
                         if (p0 := line.find(head)) >= 0), None)
            if spot is not None:
                placements.append((spot[0], spot[1], {"l": pr["l"], "h": head}))
        for j, _, rowh in sorted(placements, key=lambda x: (x[0], x[1])):
            hu["lines"][j]["rows"].append(rowh)
    return {"units": units, "part_titles": part_titles, "subpart_titles": subpart_titles,
            "derived_from": {"store_dir": os.path.abspath(store_dir), "date": date}}


def cmd_derive_hints(args):
    data = derive_hints(args.store_dir, args.date, args.regulation)
    json.dump(data, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    units = data["units"]
    n_rows = sum(len(l["rows"]) for v in units.values() for l in v["lines"])
    print(f"hints for {len(units)} units ({n_rows} row placements) as of {args.date} "
          f"-> {args.out}")


def _load_hints(path):
    if path and os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return {}


# ---------------------------------------------------------------- era survey
# Parser eras across the acquisition.gov archive downloads.  An era = one HTML
# generator lineage = one parser (its own chunk_edition_* producing the standard
# chunk rows, validated with `seam`).  The distinguishing axes are: file
# GRANULARITY (per-part / per-subpart), how SECTION BOUNDARIES are marked, whether
# cross-references are <a href> LINKS or plain text, and the CSS-class vocabulary.
#
#   era             span      granularity   headings         refs    status
#   ditaot          2021+     part file     autonumber span  links   IMPLEMENTED
#   webworks-2005   2005-18   subpart file  class=pSection   links   todo
#   webworks-2001   1999-04   subpart file  class=Heading*   links   todo
#   legacy          1990-99   part file     <B>/<H3> text     text    todo
#   fm-source       --        (.fm only)    --               --      not parseable
#
# The 1990- and 1997-labelled folders differ only in incidental heading-tag
# wrapping (bare <B> vs <H3><B>); their content markup, granularity and plain-text
# refs are identical, so they are ONE 'legacy' parser era (variant recorded).

def classify_folder(d):
    """(era, variant, html_root, n_html_files) for one archive edition folder."""
    # editions shipping actual DITA source outrank any HTML rendering they also carry
    if glob.glob(os.path.join(d, "**", "*DITA*Files*.zip"), recursive=True):
        return ("dita", "", d, 0)
    files = []
    for root, dirs, fs in os.walk(d):
        if "__MACOSX" in root:
            continue
        files.extend(os.path.join(root, f) for f in fs
                     if f.lower().endswith((".html", ".htm")))
    if not files:
        has_fm = any(f.endswith(".fm") for _, _, fs in os.walk(d) for f in fs)
        return ("fm-source" if has_fm else "empty", "", "", 0)
    names = {os.path.basename(f) for f in files}
    root = os.path.dirname(files[0])
    part_rx = re.compile(r"(FAR|AFARS)[-_](Part|PART)[-_]\d+\.html$")
    if any(part_rx.match(n) for n in names):
        pf = next(f for f in files if part_rx.match(os.path.basename(f)))
        head = open(pf, encoding="utf-8", errors="replace").read(400)
        if "legacy-compat" in head or "DC.Type" in head:
            return ("ditaot", "", edition_root(d), len(files))
        return ("agov", "", os.path.dirname(pf), len(files))   # Drupal/Word-export render
    sample = next((f for f in files
                   if re.search(r"Subpart[ _]\d+_\d|(^|[/\\])(0?5|51\d\d)\.html?$"
                                r"|05PART|AFAR\d+\.htm", f)),
                  files[len(files) // 2])
    raw = open(sample, encoding="utf-8", errors="replace").read(2500)
    # FARSite mirrors (farsite.hill.af.mil, wrapped as web/farhill-source/...) reuse
    # the HTML Transit generator but a different page structure -- own parser needed,
    # so classify distinctly: the driver then skips instead of ingesting zero rows.
    if "farhill-source" in root.replace(os.sep, "/") or "farsite.hill.af.mil" in raw:
        return ("transit-farsite", "farsite", root, len(files))
    if "legacy-compat" in raw or "DC.Type" in raw:
        # DITA-OT per-TOPIC render (acquisition.gov "copypaste" export, 2021+). The
        # same content ships as canon DITA in the GSA GitHub repos -- source it from
        # there (nightly/replay) rather than parsing this render.
        return ("ditaot-topics", "copypaste", root, len(files))
    if "HTML Transit" in raw:
        return ("transit", "", root, len(files))
    if 'name="generator" content="pandoc"' in raw:
        return ("pandoc", "", root, len(files))
    # modern acquisition.gov / agency-site Word exports (Drupal 'regnavigation' nav
    # blocks, Word Heading2/3/4 + TOCn classes, VA's indentPara render). These were
    # falling through to 'legacy' -- whose parser extracts ~nothing -- and pinning the
    # audit headline at 10-30%%. No parser yet: classify distinctly so the driver
    # skips them and the survey reports them under needs_new_parser.
    if "regnavigation" in raw \
            or re.search(r'class="(?:Heading[2-4]|TOC[1-3]|indentPara|indenta)"', raw) \
            or re.search(r'(?i)content="?Microsoft Word', raw):
        return ("agov-export", "", root, len(files))
    # GSAM's 2004-2019 site render: per-PART WebWorks files with pHeading1/2/3 heads
    if any(re.match(r"(?i)^part\d+\.html?$", n) for n in names):
        pf = next(f for f in files
                  if re.match(r"(?i)^part\d+\.html?$", os.path.basename(f)))
        if "pHeading" in open(pf, encoding="utf-8", errors="replace").read(4000):
            return ("webworks-parts", "", os.path.dirname(pf), len(files))
    if "pBody" in raw or "pSection" in raw:
        return ("webworks-2005", "", root, len(files))
    if 'class="Body"' in raw or "WebWorks" in raw:
        return ("webworks-2001", "", root, len(files))
    # the single sample can be an unmarked TOC/blank page while the CONTENT files are
    # DITA-OT topic renders (GSAM: 'GSAM-501.107.html' + DC.Type) -- probe a spread of
    # other files for era signatures before conceding 'legacy'
    probe = [f for f in files
             if re.search(r"(?i)[a-z]+[-_]part[-_]?\d|\d{3,4}\.\d", os.path.basename(f))][:6]
    for f2 in probe + files[::max(1, len(files) // 12)][:12] + files[-2:]:
        head = open(f2, encoding="utf-8", errors="replace").read(2500)
        if "legacy-compat" in head or "DC.Type" in head:
            return ("ditaot-topics", "copypaste", root, len(files))
        if "regnavigation" in head:
            return ("agov-export", "", root, len(files))
    variant = "H-headings" if re.search(r"<H[1-4]", raw, re.I) else "B-headings"
    return ("legacy", variant, root, len(files))


def cmd_survey(args):
    """Classify every folder in an archives download dir into parser eras."""
    eras = {}
    out = {}
    if not os.path.isdir(args.archives_dir):
        print(f"archives dir not found: {args.archives_dir}")
        sys.exit(2)
    for d in sorted(os.listdir(args.archives_dir)):
        p = os.path.join(args.archives_dir, d)
        if not os.path.isdir(p):
            continue
        era, variant, root, n = classify_folder(p)
        meta = parse_meta(p, args.regulation)   # authoritative dates for EVERY era
        rec = {"era": era, "variant": variant,
               "html_root": os.path.relpath(root, p) if root else "",
               "html_files": n}
        if meta.get("fac"):
            rec["fac"], rec["effective_date"] = meta["fac"], meta["effective_date"]
        out[d] = rec
        eras.setdefault(era, []).append(d)
    outpath = args.out or eras_path(args.regulation)
    json.dump(out, open(outpath, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    for era in sorted(eras):
        v = eras[era]
        print(f"{era:15s} {len(v):4d}   {v[0]}  …  {v[-1]}")
    print(f"-> {outpath}")


# ---------------------------------------------------------------- commands
def default_cfg(regulation="FAR"):
    profile = ensure_profile(regulation)
    bd = profile.get("bottom_depth", 1)
    cfg = {"regulation": regulation, "url_template": profile["url_template"],
           "ditamap": profile["ditamap"],
           "bottom_depth": bd, "bottom_level": "paragraph" if bd else "section"}
    if regulation == "FAR":                    # FAR may carry local overrides
        cfgp = os.path.join(HERE, "pipeline.config.json")
        c = json.load(open(cfgp, encoding="utf-8")) if os.path.exists(cfgp) else {}
        cfg["url_template"] = c.get("url_template", cfg["url_template"])
    return cfg


def cmd_meta(args):
    print(json.dumps(parse_meta(args.edition_dir, args.regulation), indent=2))


def _sec_body_len(blocks):
    """Visible-text length of a section's discovered block list."""
    return len(X.norm(" ".join(b.text_content() for b in blocks
                               if isinstance(b.tag, str))))


def declared_sections(edition_dir, era, body_min=40):
    """The classifier-free completeness MANIFEST: section numbers the PUBLISHER declared
    with substantive body text (> body_min visible chars), discovered by the era's OWN
    section splitter but WITHOUT the row builder's downstream near-empty / grouping /
    end-marker filtering. Diffing this against parsed rows yields PROVABLE drops --
    a section the source wrote text for that produced no row -- owing nothing to the
    conservation shingle-pool or any skip-by-design classifier.

    Returns a set of section numbers. `dita` returns empty: its source is a DITA zip
    (no HTML to segment), validated instead by the cross-rendering differential."""
    real = set()

    def take_lg(root, files, parse, relaxed):
        for f in files:
            try:
                doc = parse(f)
            except Exception:
                continue
            body = doc.find(".//body")
            if body is None:
                body = doc
            if relaxed:                              # farsite strips script/comment
                for bad in body.xpath(".//script | .//comment()"):
                    p = bad.getparent()
                    if p is not None:
                        p.remove(bad)
            secs, _ = _lg_sections(body, relaxed=relaxed)
            for s in secs:
                if NUMERIC.match(s["num"]) and _sec_body_len(s["blocks"]) > body_min:
                    real.add(s["num"])

    if era in ("ditaot", "ditaot-topics"):
        for f in _dtt_files(edition_dir):
            try:
                root = lxml.html.parse(f).getroot()
            except Exception:
                continue
            for a in root.iter("article"):
                num, title = _title_of(a)
                num = re.sub(r"(?i)^PGI\s+", "", num or "")
                if not num:
                    m = re.match(r"(?i)^(?:PGI\s+)?(\d{3,4}\.\d[\d.-]*)\b", title or "")
                    num = m.group(1) if m else ""
                if not (num and NUMERIC.match(num)):
                    continue
                b = _own_body(a)
                if b is not None and len(X.norm(" ".join(b.itertext()))) > body_min:
                    real.add(num)
    elif era == "transit-farsite":
        take_lg(_fs_root(edition_dir), _fs_files(_fs_root(edition_dir)),
                _fs_parse, True)
    elif era == "transit":
        root = tr_root(edition_dir)
        if _tr_files(root):
            take_lg(root, _tr_files(root), _lg_parse, False)
        else:                                        # non-AFARS: farsite-style delegate
            take_lg(_fs_root(edition_dir), _fs_files(_fs_root(edition_dir)),
                    _fs_parse, True)
    elif era == "legacy":
        root = lg_root(edition_dir)
        take_lg(root, _lg_files(root), _lg_parse, False)
    elif era in ("webworks-2005", "webworks-2001"):
        root = ww_root(edition_dir)
        for f in _ww_files(root):
            try:
                doc = lxml.html.parse(f).getroot()
            except Exception:
                continue
            if era == "webworks-2005":
                body = doc.find(".//body")
                secs = _ww_sections(body)[0] if body is not None else []
            else:
                cont = doc.find(".//blockquote")
                if cont is None:
                    cont = doc.find(".//body")
                secs = _w1_sections(cont)[0] if cont is not None else []
            for s in secs:
                if NUMERIC.match(s["num"]) and _sec_body_len(s["blocks"]) > body_min:
                    real.add(s["num"])
    elif era == "webworks-parts":
        for f in _wp_files(edition_dir):
            try:
                body = lxml.html.parse(f).getroot().find(".//body")
            except Exception:
                continue
            if body is None:
                continue
            for s in _wp_sections(body)[0]:
                if NUMERIC.match(s["num"]) and _sec_body_len(s["blocks"]) > body_min:
                    real.add(s["num"])
    elif era == "agov":
        part_rx = re.compile(r"AFARS[-_](Part|PART)[-_]\d+\.html$")
        for f in sorted(glob.glob(os.path.join(edition_dir, "**", "*.html"),
                                  recursive=True)):
            if not part_rx.match(os.path.basename(f)) or "__MACOSX" in f:
                continue
            try:
                body = lxml.html.parse(f).getroot().find(".//body")
            except Exception:
                continue
            if body is None:
                continue
            for s in _ag_sections(body)[0]:
                if NUMERIC.match(s["num"]) and _sec_body_len(s["blocks"]) > body_min:
                    real.add(s["num"])
    elif era == "canon":
        # publisher-declared = any topic file whose title autonumber is a numeric section
        # and that carries a body paragraph -- covers BOTH eCFR 'Section_*.dita' and the
        # numeric agencies' '5801.101.dita' naming. Skip part/subpart/chapter containers.
        # Raw-regex scan (no DTD resolution needed).
        dita_dir = _ecfr_dita_dir(edition_dir)
        anum = re.compile(r'(?is)<ph[^>]*\bprops="autonumber"[^>]*>\s*([\d.\-]+)\s*</ph>')
        for p in glob.glob(os.path.join(dita_dir, "*.dita")):
            b = os.path.basename(p)
            if re.match(r"(?i)(Part|Subpart|Chapter|Subchapter)[_-]", b):
                continue
            raw = open(p, encoding="utf-8", errors="replace").read()
            m = anum.search(raw)
            if m and NUMERIC.match(m.group(1)) \
                    and re.search(r"(?is)<(conbody|body|refbody|taskbody)\b.*?<p\b", raw):
                real.add(m.group(1))
    return real


ERA_CHUNKERS = {"dita": chunk_edition_dita,
                "ditaot": chunk_edition_ditaot,
                "ditaot-topics": chunk_edition_ditaot_topics,
                "agov": chunk_edition_agov,
                "transit": chunk_edition_transit,
                "transit-farsite": chunk_edition_farsite,
                "webworks-2005": chunk_edition_webworks2005,
                "webworks-parts": chunk_edition_webworks_parts,
                "webworks-2001": chunk_edition_webworks2001,
                "legacy": chunk_edition_legacy,
                "canon": chunk_edition_canon}


def cmd_chunk(args):
    cfg = default_cfg(args.regulation)
    era = args.era or classify_folder(args.edition_dir)[0]
    if era not in ERA_CHUNKERS:
        sys.exit(f"no parser for era '{era}' yet (have: {', '.join(ERA_CHUNKERS)})")
    meta = parse_meta(args.edition_dir, args.regulation)
    cfg["source_version"] = args.source_version or meta["source_version"]
    cfg["pipeline_version"] = _pipeline_rev()
    # hints: ditaot needs full line/row skeletons; webworks-2005 reads structure from
    # its own class depths and only borrows breadcrumb titles, so hints are optional.
    if args.hints_store_dir:
        date = args.hints_date or meta["effective_date"] or "0000-00-00"
        st = Store(args.hints_store_dir, cfg["regulation"])
        floor = min((e["effective_date"] for e in st.editions), default=date)
        date = max(date, floor)
        hints = derive_hints(args.hints_store_dir, date, cfg["regulation"])
        print(f"hints derived from {args.hints_store_dir} as of {date}")
    else:
        hints = _load_hints(args.hints) if os.path.exists(args.hints) else {}
    if era == "ditaot" and not hints:
        print(f"WARNING: no hints loaded ({args.hints}) -- run derive-hints first; "
              f"falling back to class-based structure rules")
    rows, manifest = ERA_CHUNKERS[era](args.edition_dir, cfg, hints)
    json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[{era}] chunked {args.edition_dir}: {len(rows)} rows "
          f"({len(manifest['processed'])} units, {len(manifest['skipped'])} skipped) "
          f"-> {args.out}")
    print(f"meta: {meta}")


def cmd_seam(args):
    """HASH_FIELDS comparison: adapter rows vs the store's as_of(date) view."""
    rows = json.load(open(args.rows, encoding="utf-8"))
    st = Store(args.store_dir, args.regulation)
    ref = {(r["citation"], r.get("alternate", "")): r for r in st.as_of(args.date)}
    got = {}
    for r in rows:
        got.setdefault((r["citation"], r.get("alternate", "")), r)
    only_new = sorted(set(got) - set(ref))
    only_ref = sorted(set(ref) - set(got))
    both = sorted(set(got) & set(ref))
    same = [k for k in both if content_hash(got[k]) == content_hash(ref[k])]
    diff = [k for k in both if content_hash(got[k]) != content_hash(ref[k])]
    per_field = {}
    samples = []
    for k in diff:
        for f in HASH_FIELDS:
            if got[k].get(f) != ref[k].get(f):
                per_field[f] = per_field.get(f, 0) + 1
                if len(samples) < args.samples:
                    a, b = json.dumps(ref[k].get(f), ensure_ascii=False), \
                           json.dumps(got[k].get(f), ensure_ascii=False)
                    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y),
                             min(len(a), len(b)))
                    samples.append({"key": list(k), "field": f,
                                    "store": a[max(0, i-80):i+120],
                                    "adapter": b[max(0, i-80):i+120]})
    print(f"identities: adapter={len(got)}  store@{args.date}={len(ref)}  both={len(both)}")
    print(f"  hash-identical: {len(same)}   hash-DIFFERENT: {len(diff)}")
    print(f"  only-in-adapter: {len(only_new)}   only-in-store: {len(only_ref)}")
    print(f"  per-field mismatches: {per_field}")
    report = {"date": args.date, "identical": len(same), "different": len(diff),
              "only_in_adapter": [list(k) for k in only_new],
              "only_in_store": [list(k) for k in only_ref],
              "per_field": per_field,
              "diff_keys": [list(k) for k in diff],
              "samples": samples}
    out = args.report or "seam_report.json"
    json.dump(report, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"  report -> {out}")


# ---------------------------------------------------------------- cosmetic collapse
# Ingesting a DIFFERENT-source era (FrameMaker 2005 vs the DITA store) makes the same
# legal text hash differently at the seam (dash glyphs, U.S.C./CFR spacing, run-in label
# spacing), which would fabricate ~thousands of version rows that reflect typography, not
# amendments. `--collapse-cosmetic` uses a canonicalizer as a CLASSIFIER (not a rewriter):
# a snapshot chunk that is cosmetic-equal to the identity's earliest existing row is
# SNAPPED to that row's exact content so the merge engine extends it backward (no new
# row); a chunk that differs meaningfully is ingested VERBATIM as a real historical
# version.  Every snap is logged for audit.  Conservative by design: ANY difference in a
# structural/metadata hash field (type, instrument, date, prescribed_by, reserved,
# end_marker, images) counts as meaningful -- only free-text typography collapses.

_DASH_MAP = {ord(c): "-" for c in "—–‐‑‒―−﹘－­"}   # incl. soft hyphen (legacy '14.201\xad6')
_TITLE_FIELDS = ["part_title", "subpart_title", "section_title", "subsection_title"]
_STRUCT_FIELDS = [f for f in HASH_FIELDS if f not in ["text"] + _TITLE_FIELDS]


def _canon_text(s, drop_quotes):
    """Canonicalize away known cross-source typographic artifacts for COMPARISON only."""
    s = (s or "").translate(_DASH_MAP)
    s = s.replace("-", "")                       # hyphens entirely: legacy '--' == em-dash,
                                                 # and webworks-2001 drops them ('52.2364')
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    if drop_quotes:                              # 2005 quoted defined terms; 2021 dropped them
        s = s.replace('"', "").replace("'", "")
    return re.sub(r"\s+", "", s)                 # collapses whitespace incl. U.S.C. spacing, run-in labels


def _cosmetic_equal(a, b, drop_quotes):
    """True iff a and b are the SAME legal content differing only in cross-source
    typography: every structural/metadata hash field is exactly equal, and the free-text
    fields (text + the four titles) match after canonicalization."""
    for f in _STRUCT_FIELDS:
        if a.get(f) != b.get(f):
            return False
    if _canon_text(a.get("text", ""), drop_quotes) != _canon_text(b.get("text", ""), drop_quotes):
        return False
    return all(_canon_text(a.get(f, "") or "", drop_quotes)
               == _canon_text(b.get(f, "") or "", drop_quotes) for f in _TITLE_FIELDS)


def collapse_cosmetic(rows, store, date, drop_quotes=False):
    """Snap each chunk that differs only cosmetically from the store row the merge would
    ABUT -- the row in force at `date`, or (backfill) the nearest row starting after it --
    to that row's exact content, so the merge extends/keeps it instead of inserting a
    typography-only version. Returns (new_rows, collapsed_keys).

    Checking BOTH the in-force row and the next-newer row is what makes a multi-edition
    backfill correct AND order-independent: a 2005 edition that first introduces text
    which matches the FUTURE DITA rendering (cosmetically) folds into that later row
    rather than opening a spurious boundary at the 2005->2021 seam; a later same-era
    edition that matches the row already in force folds into it. Chunks with no existing
    identity, or a genuinely different one, pass through verbatim."""
    from chunker.store import in_force
    chains = store.chains()
    out, collapsed = [], []
    for r in rows:
        chain = chains.get((r["citation"], r.get("alternate", "")), [])
        in_force_row = next((x for x in chain if in_force(x, date)), None)
        later_row = next((x for x in chain if x["effective_from"] > date), None)
        target = next((c for c in (in_force_row, later_row)
                       if c is not None and content_hash(r) != content_hash(c)
                       and _cosmetic_equal(r, c, drop_quotes)), None)
        if target is not None:
            snap = dict(r)
            for f in HASH_FIELDS:                 # snap so hashes match -> extend/keep, no new version
                snap[f] = target.get(f)
            out.append(snap)
            collapsed.append((r["citation"], r.get("alternate", "")))
        else:
            out.append(r)
    return out, collapsed


def cmd_ingest(args):
    rows = json.load(open(args.rows, encoding="utf-8"))
    st = Store(args.store_dir, args.regulation)
    # date / source-version / commit default from the edition folder's authoritative
    # metadata (archive_dates.json) when --edition-dir is given; explicit flags override.
    date, source_version, commit = args.date, args.source_version, args.commit
    if args.edition_dir:
        meta = parse_meta(args.edition_dir)
        date = date or meta["effective_date"]
        source_version = source_version or meta["source_version"]
        commit = commit or os.path.basename(os.path.normpath(args.edition_dir))
    if not date or not source_version:
        sys.exit("ingest needs --date and --source-version (or --edition-dir to derive them)")
    if args.collapse_cosmetic:
        n0 = len(rows)
        rows, collapsed = collapse_cosmetic(rows, st, date, drop_quotes=args.collapse_drop_quotes)
        log = {"rows": args.rows, "date": date, "source_version": source_version,
               "drop_quotes": args.collapse_drop_quotes,
               "collapsed_count": len(collapsed),
               "collapsed": [list(k) for k in collapsed]}
        logpath = args.collapse_log or (os.path.splitext(args.rows)[0] + "_collapsed.json")
        json.dump(log, open(logpath, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print(f"collapse-cosmetic: {len(collapsed)}/{n0} chunks snapped to existing text "
              f"(extend-backward, no new version) -> audit log {logpath}")
    stats = st.merge_snapshot(rows, date, source_version,
                              source=SOURCE, source_commit=commit)
    st.save()
    keep = {k: v for k, v in stats.items() if k != "sections_changed" and v}
    print(f"merged {args.rows} at {date} ({source_version}): {keep}")
    print(f"sections_changed: {len(stats['sections_changed'])}")
    problems = st.verify()
    print(f"invariants: {'OK' if not problems else problems[:10]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("meta"); m.add_argument("edition_dir")
    m.add_argument("--regulation", default="FAR"); m.set_defaults(fn=cmd_meta)
    v = sub.add_parser("survey")
    v.add_argument("archives_dir")
    v.add_argument("--regulation", default="FAR")
    v.add_argument("--out", default="")
    v.set_defaults(fn=cmd_survey)
    d = sub.add_parser("derive-hints")
    d.add_argument("--store-dir", required=True); d.add_argument("--date", required=True)
    d.add_argument("--regulation", default="FAR")
    d.add_argument("--out", default=os.path.join(CACHE_DIR, "archive_hints.json"))
    d.set_defaults(fn=cmd_derive_hints)
    c = sub.add_parser("chunk")
    c.add_argument("edition_dir"); c.add_argument("-o", "--out", required=True)
    c.add_argument("--regulation", default="FAR")
    c.add_argument("--era", default="", help="override era detection")
    c.add_argument("--source-version", default="")
    c.add_argument("--hints", default=os.path.join(CACHE_DIR, "archive_hints.json"))
    c.add_argument("--hints-store-dir", default="",
                   help="derive hints live from this store (as of --hints-date or the "
                        "edition's own effective date, clamped to the store's floor)")
    c.add_argument("--hints-date", default="")
    c.set_defaults(fn=cmd_chunk)
    s = sub.add_parser("seam")
    s.add_argument("rows"); s.add_argument("--store-dir", required=True)
    s.add_argument("--date", required=True); s.add_argument("--regulation", default="FAR")
    s.add_argument("--samples", type=int, default=25); s.add_argument("--report", default="")
    s.set_defaults(fn=cmd_seam)
    i = sub.add_parser("ingest")
    i.add_argument("rows"); i.add_argument("--store-dir", required=True)
    i.add_argument("--edition-dir", default="",
                   help="derive --date/--source-version/--commit from this edition folder's "
                        "authoritative metadata (archive_dates.json)")
    i.add_argument("--date", default=""); i.add_argument("--source-version", default="")
    i.add_argument("--commit", default=""); i.add_argument("--regulation", default="FAR")
    i.add_argument("--collapse-cosmetic", action="store_true",
                   help="cross-source seam: snap chunks that differ from the store only by "
                        "typography (dashes/spacing) to the existing text (extend backward, "
                        "no new version); ingest real differences verbatim. Writes an audit log.")
    i.add_argument("--collapse-drop-quotes", action="store_true",
                   help="also treat quotes-around-defined-terms differences as cosmetic "
                        "(2005 quoted terms, 2021 dropped them). Off by default (kept as a "
                        "real difference).")
    i.add_argument("--collapse-log", default="",
                   help="path for the collapse audit log (default: <rows>_collapsed.json)")
    i.set_defaults(fn=cmd_ingest)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
