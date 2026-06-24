#!/usr/bin/env python3
"""Parse the FAR Subpart 5.2 cluster DITA files into a Postgres seed file.

Stdlib only (xml.etree) -- no pip installs, runs on Python 3.14.
Emits seed.sql with: content_items, chunks, relationships.

Key design points (see Ingestion design doc):
  * content_item address comes from <ph props="autonumber">, NOT the XML id.
  * dense content_items (every paragraph) / sparse chunks (section + letter level).
  * <xref> -> typed relationship with confidence tier; trailing "(a)(2)" upgrades
    a high-confidence section link to a medium-confidence paragraph link.
"""
import os, re, sys, glob
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DITA = os.path.dirname(HERE)  # dita files live one level up

# 9-node cluster: core 6 + the 3 leaf targets that close the question paths.
FILES = ["5.201", "5.202", "5.203", "5.205", "5.207", "12.603",
         "5.101", "6.302-2", "16.505"]

def q(s):
    """SQL-quote a string (or NULL)."""
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"

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
    raw = open(path, encoding="utf-8").read()
    raw = re.sub(r"<!DOCTYPE.*?>", "", raw, flags=re.S)  # drop DTD ref
    root = ET.fromstring(raw)
    return root.find(".//concept")

def para_text(p):
    """Readable text of a <p>, dropping the leading autonumber label but keeping
    inline xref / emphasis text."""
    parts = []
    if p.text:
        parts.append(p.text)
    for child in p:
        tag = child.tag
        is_auto = (tag == "ph" and child.get("props") == "autonumber")
        if not is_auto:
            parts.append("".join(child.itertext()))
        if child.tail:
            parts.append(child.tail)
    return norm("".join(parts))

# ---- accumulators -------------------------------------------------------
items = []      # (id, type, far, parent, title, breadcrumb, depth, retrievable)
chunks = []     # (chunk_id, item_id, far, title, breadcrumb, canonical, enriched)
rels = []       # (from, to, type, conf, anchor, raw, review)
seen_struct = set()

def add_struct(cid, typ, far, parent, title, crumb):
    if cid in seen_struct:
        return
    seen_struct.add(cid)
    items.append((cid, typ, far, parent, title, crumb, 0, False))

def collect_xrefs(p, from_id, own_text):
    for x in p.iter("xref"):
        href = x.get("href") or ""
        scope = x.get("scope")
        anchor = norm("".join(x.itertext()))
        # external (USC, sam.gov, etc.)
        if scope == "external" or href.startswith("http"):
            rels.append((from_id, None, "external_reference", "external", anchor, href, False))
            continue
        # internal: target id is the fragment after '#'
        tgt = href.split("#")[-1] if "#" in href else None
        if not tgt:
            continue
        # coarse: Subpart_x / Part_x links
        if tgt.startswith("FAR_Subpart") or tgt.startswith("FAR_Part"):
            rels.append((from_id, tgt, "references", "coarse", anchor, href, False))
            continue
        # trailing "(a)(2)" immediately after </xref> -> paragraph-level (medium)
        suffix = re.match(r"\s*((?:\([A-Za-z0-9]+\))+)", x.tail or "")
        if suffix:
            toks = re.findall(r"\(([A-Za-z0-9]+)\)", suffix.group(1))
            to_item = tgt + "_" + "_".join(toks)
            rels.append((from_id, to_item, "references", "medium",
                         anchor + suffix.group(1).strip(), href, False))
        else:
            rels.append((from_id, tgt, "references", "high", anchor, href, False))
    # prose ranges / relative refs -> low confidence, review_required
    for m in re.finditer(r"\([a-z]\)\s+through\s+\([a-z]\)(\s+of this section)?", own_text):
        rels.append((from_id, None, "references", "low", m.group(0), "prose-range", True))
    for m in re.finditer(r"paragraph\s+\([a-z0-9]+\)(\([a-z0-9]+\))*\s+of this section", own_text):
        rels.append((from_id, None, "references", "low", m.group(0), "prose-relative", True))

def parse_section(far):
    c = load_concept(far)
    if c is None:
        return
    sec_id = c.get("id")
    title_el = c.find("./title")
    num_ph = title_el.find(".//ph[@props='autonumber']")
    sec_num = norm(num_ph.text) if num_ph is not None else far
    full_title = norm("".join(title_el.itertext()))
    sec_title = norm(full_title.replace(sec_num, "", 1))

    # structural ancestors: Part N > Subpart N.x
    part = sec_num.split(".")[0]
    after = sec_num.split(".")[1] if "." in sec_num else ""
    sub = f"{part}.{after[0]}" if after else part
    part_id = f"FAR_PART_{part}"
    sub_id = f"FAR_SUBPART_{part}_{after[0]}" if after else part_id
    add_struct(part_id, "part", part, None, None, f"Part {part}")
    sub_crumb = f"Part {part} › Subpart {sub}"
    add_struct(sub_id, "subpart", sub, part_id, None, sub_crumb)

    sec_crumb = f"{sub_crumb} › {sec_num} {sec_title}"
    items.append((sec_id, "section", sec_num, sub_id, sec_title, sec_crumb, 0, True))

    section_texts = []  # for the whole-section chunk

    conbody = c.find(".//conbody")
    if conbody is None:
        return

    # intro <p> directly under conbody (section-level prose, e.g. 5.202 lead-in)
    for p in conbody.findall("./p"):
        t = para_text(p)
        if t:
            section_texts.append(t)
            collect_xrefs(p, sec_id, t)

    def walk(ol, parent_toks, parent_id, parent_crumb):
        for idx, li in enumerate(ol.findall("./li")):
            p = li.find("./p")
            ph = p.find("./ph[@props='autonumber']") if p is not None else None
            label = norm(ph.text) if (ph is not None and norm(ph.text)) else f"(p{idx})"
            t = tok(label) or f"p{idx}"
            toks = parent_toks + [t]
            cid = sec_id + "_" + "_".join(toks)
            addr = sec_num + "".join(f"({t})" for t in toks)
            own = para_text(p) if p is not None else ""
            crumb = f"{parent_crumb} › {label}"
            items.append((cid, "paragraph", addr, parent_id, None, crumb, len(toks), True))
            if own:
                section_texts.append(f"{label} {own}")
                collect_xrefs(p, cid, own)
            # gather this letter's full text (own + descendants) for a letter chunk
            letter_chunk_texts = []
            if own:
                letter_chunk_texts.append(f"{label} {own}")
            for sub_ol in li.findall("./ol"):
                walk_collect(sub_ol, toks, cid, crumb, letter_chunk_texts)
            # emit a chunk only at the top lettered level (depth 1)
            if len(toks) == 1:
                ctext = norm(" ".join(letter_chunk_texts)) or own or label
                enr = f"{sec_crumb} › {label} | {ctext}"
                chunks.append((cid + "__c", cid, addr, sec_title, crumb, ctext, enr))

    def walk_collect(ol, parent_toks, parent_id, parent_crumb, sink):
        """Like walk but also appends descendant text into `sink` (the letter chunk)."""
        for idx, li in enumerate(ol.findall("./li")):
            p = li.find("./p")
            ph = p.find("./ph[@props='autonumber']") if p is not None else None
            label = norm(ph.text) if (ph is not None and norm(ph.text)) else f"(p{idx})"
            t = tok(label) or f"p{idx}"
            toks = parent_toks + [t]
            cid = sec_id + "_" + "_".join(toks)
            addr = sec_num + "".join(f"({tt})" for tt in toks)
            own = para_text(p) if p is not None else ""
            crumb = f"{parent_crumb} › {label}"
            items.append((cid, "paragraph", addr, parent_id, None, crumb, len(toks), True))
            if own:
                sink.append(f"{label} {own}")
                collect_xrefs(p, cid, own)
            for sub_ol in li.findall("./ol"):
                walk_collect(sub_ol, toks, cid, crumb, sink)

    for ol in conbody.findall("./ol"):
        walk(ol, [], sec_id, sec_crumb)

    # whole-section chunk
    sec_text = norm(" ".join(section_texts))
    enr = f"{sec_crumb} | {sec_text}"
    chunks.append((sec_id + "__c", sec_id, sec_num, sec_title, sec_crumb, sec_text, enr))

# ---- run ----------------------------------------------------------------
def all_files():
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(DITA, "*.dita")))

def reset():
    items.clear(); chunks.clear(); rels.clear(); seen_struct.clear()

def run(files):
    """Parse the given list of section names; return (failures, skipped)."""
    reset()
    failures, skipped = [], []
    for far in files:
        n_before = len(items)
        try:
            parse_section(far)
            if len(items) == n_before:
                skipped.append(far)        # no <concept> / nothing extracted
        except Exception as e:
            failures.append((far, repr(e)))
    return failures, skipped

if __name__ == "__main__":
    sel = all_files() if os.environ.get("FAR_ALL") else FILES
    fails, skips = run(sel)
    sys.stderr.write(f"parsed {len(sel)} files; "
                     f"failures={len(fails)} skipped={len(skips)}\n")
    outp = os.path.join(HERE, "seed.sql")
    with open(outp, "w", encoding="utf-8") as f:
        f.write("-- generated by parse_to_sql.py\nBEGIN;\n")
        f.write("\n-- content_items\n")
        for (cid, typ, far, par, title, crumb, depth, retr) in items:
            f.write(f"INSERT INTO content_items VALUES ({q(cid)},{q(typ)},{q(far)},"
                    f"{q(par)},{q(title)},{q(crumb)},{depth},{str(retr).upper()});\n")
        f.write("\n-- chunks\n")
        for (chid, iid, far, title, crumb, canon, enr) in chunks:
            f.write(f"INSERT INTO chunks (chunk_id,content_item_id,far_address,title,"
                    f"breadcrumb,canonical_text,enriched_text) VALUES ({q(chid)},{q(iid)},"
                    f"{q(far)},{q(title)},{q(crumb)},{q(canon)},{q(enr)});\n")
        f.write("\n-- relationships\n")
        for (frm, to, typ, conf, anchor, raw, rev) in rels:
            f.write(f"INSERT INTO relationships (from_item,to_item,rel_type,confidence,"
                    f"anchor_text,target_raw,review_required) VALUES ({q(frm)},{q(to)},"
                    f"{q(typ)},{q(conf)},{q(anchor)},{q(raw)},{str(rev).upper()});\n")
        f.write("COMMIT;\n")
    sys.stderr.write(f"items={len(items)} chunks={len(chunks)} rels={len(rels)} wrote {outp}\n")
