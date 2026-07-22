#!/usr/bin/env python3
"""Configurable DITA chunker (stage 1 of the pipeline).

Reuses the proven helpers in ../extract_json.py but generalizes:
  * chunk at every level from the file's own unit (section/subsection) down to a
    configurable bottom level (paragraph .. subunit-depth-N);
  * carry a `regulation` field (FAR / DFARS / …);
  * scan a configurable input folder and emit a manifest of processed + skipped files.
"""
import os, re, sys, json, glob
import xml.etree.ElementTree as ET
from chunker import extract_json as X   # reuse norm/tok, flatten_*, collect_refs, components, href_to_citation
from chunker import changelog           # change track: recover [CaseNumber]/[Why] markers ET drops

# Level ladder.  Indices 0..3 come from the citation number; 4+ from the (a)(1)(i)… chain.
PARA_LEVELS = ["paragraph", "subparagraph",
               "subunit-depth-1", "subunit-depth-2", "subunit-depth-3", "subunit-depth-4"]
LEVEL_DEPTH = {"section": 0, "subsection": 0,
               **{name: i + 1 for i, name in enumerate(PARA_LEVELS)}}
NUMERIC = re.compile(r"^\d+\.\d+(-\d+)?$")
MIN_TEXT = 40

def level_name(depth):
    return PARA_LEVELS[depth - 1] if depth >= 1 else "section"

# ---------- alternates ----------
# Alternates are variant clause versions that follow the "(End of clause)"/"(End of provision)"
# marker, gathered in a trailing <section> (usually outputclass="Alternate"; 5 files use a plain
# <section>). Each begins with an opener <p> whose leading italic is "Alternate <roman>".
# Canonical terminator per outputclass — the source text varies ("(End of Provision)", "End of
# clause"), so we normalize from the authoritative @outputclass for a clean, reusable delimiter.
END_MARKER = {"Endofclause": "(End of clause)", "Endofprovision": "(End of provision)"}
ALT_OPENER = re.compile(r"^Alternate\s+([IVXLCDM]+)\b", re.I)
ALT_DATE   = re.compile(r"([A-Za-z]{3,9}\.?)\s*(\d{4})")                # "(Feb 2000)" / "(Sept1989)"
PRESCRIBED = re.compile(r"As prescribed in\s+(\d+\.\d+(?:-\d+)?(?:\s*\([A-Za-z0-9]+\))*)", re.I)
ALT_BLOCKS = ("p", "table", "simpletable", "ol", "ul", "fig", "image")

def _opener_roman(el):
    """If this <p> opens an alternate, return its roman numeral (upper-cased); else None.
    Matched on the flattened text so all markup variants are caught: '<i>Alternate I</i> (date)',
    '<i>Alternate I (date)</i>', and bare '<p>Alternate I <ph>(date)</ph>'. Body paragraphs that
    merely mention an alternate (e.g. '(2) Alternate I (Dec 2023)') flatten with a leading label or
    checkbox, so they don't start with 'Alternate' and are correctly rejected."""
    if el.tag != "p":
        return None
    m = ALT_OPENER.match(X.flatten_p(el, ""))      # url unused by flatten_p; "" is safe
    return m.group(1).upper() if m else None

def find_end_and_alt(conbody):
    """Return (end_marker_text, end_marker_el, alt_section_el) — '', None, None when absent.
    The marker is canonicalized to '(End of clause)'/'(End of provision)'. Detection is by @outputclass
    OR the literal text, but the canonical value is taken from the TEXT — the source occasionally mis-tags
    @outputclass (e.g. outputclass='Endofclause' over text '(End of provision)'), and the text is right.
    The alternate section is the first trailing <section> that is outputclass='Alternate' or holds an opener."""
    children = list(conbody)
    end_text, end_el, end_idx = "", None, -1
    for i, ch in enumerate(children):
        if ch.tag != "p":
            continue
        low = X.norm("".join(ch.itertext())).lower()
        if (ch.get("outputclass") or "") in END_MARKER or "end of clause" in low or "end of provision" in low:
            end_text = ("(End of provision)" if "provision" in low else
                        "(End of clause)" if "clause" in low else END_MARKER.get(ch.get("outputclass"), ""))
            end_el, end_idx = ch, i
            break
    alt = None
    for ch in (children[end_idx + 1:] if end_idx >= 0 else children):
        if ch.tag == "section" and (ch.get("outputclass") == "Alternate"
                                    or any(_opener_roman(p) for p in ch)):
            alt = ch
            break
    return end_text, end_el, alt

# The trailing Alternate <section>: either outputclass="Alternate" (105 files) or a plain <section>
# whose content opens with the [Alt] Start marker (5 files). No nested <section>, so `.*?</section>`
# stops at its own close.
ALT_SECTION_RE = re.compile(
    r'<section\b[^>]*\boutputclass="Alternate".*?</section>'
    r'|<section\b[^>]*>\s*<\?FM MARKER \[Alt\] Start.*?</section>', re.S)

def strip_alternates(raw):
    """Remove the trailing Alternate <section> from a clause's raw DITA. Alternates are their own
    (parser-only) chunks, so the base-unit LLM audit must NOT see their text — otherwise it reports
    references that live inside the alternate substitute text and mis-attributes them to the base
    clause (often tagged with the containing alternate's number). No-op when there's no alternate."""
    return ALT_SECTION_RE.sub("", raw, count=1)

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
def roman_to_arabic(r):
    """'I'->'1', 'IV'->'4', 'V'->'5'. Returns the input unchanged if it isn't roman."""
    s, total, prev = r.upper(), 0, 0
    for ch in reversed(s):
        v = _ROMAN.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return str(total) if total else r

def alt_spans(section):
    """Split the alternate <section> into one span per alternate: [{roman, nodes}]. Each opener <p>
    starts a span that runs to the next opener, so multi-paragraph substitute/add alternates stay
    whole. Returns [] when section is None."""
    spans, cur = [], None
    for ch in (section if section is not None else []):
        if ch.tag not in ALT_BLOCKS:
            continue
        roman = _opener_roman(ch)
        if roman:
            cur = {"roman": roman, "nodes": [ch]}
            spans.append(cur)
        elif cur is not None:
            cur["nodes"].append(ch)
    return spans

def alt_meta(nodes, url):
    """(date, prescribed_by) parsed from an alternate's opener paragraph."""
    opener = X.flatten_p(nodes[0], url)
    lead = opener.split(".", 1)[0]                       # "Alternate I (Feb 2000)" — date lives here
    dm, pm = ALT_DATE.search(lead), PRESCRIBED.search(opener)
    date = X.norm(f"{dm.group(1)} {dm.group(2)}") if dm else ""     # normalize "(Sept1989)" spacing
    return date, (pm.group(1).replace(" ", "") if pm else "")

def base_meta(conbody):
    """(date, prescribed_by) for a clause/provision base unit, from its opening: the prefatory
    'As prescribed in X, insert the following clause:' and the dated SmCaps title. Scans only the
    top few paragraphs so a stray 'as prescribed in' deeper in the body can't be mis-attributed."""
    date, prescribed_by = "", ""
    for p in [c for c in conbody if c.tag == "p"][:4]:
        t = X.norm("".join(p.itertext()))
        if not prescribed_by:
            m = PRESCRIBED.search(t)
            if m:
                prescribed_by = m.group(1).replace(" ", "")
        if not date and "SmCaps" in (p.get("outputclass") or ""):   # the dated title line
            dm = ALT_DATE.search(t)
            if dm:
                date = X.norm(f"{dm.group(1)} {dm.group(2)}")
    return date, prescribed_by

def instrument_of(conbody, end_marker):
    """Functional instrument: 'clause' | 'provision' | '' (ordinary regulatory section).
    end_marker is authoritative; fall back to the prefatory 'insert/use the following clause|
    provision' line for the few clause files that omit the terminator."""
    if end_marker == "(End of clause)":
        return "clause"
    if end_marker == "(End of provision)":
        return "provision"
    for p in [c for c in conbody if c.tag == "p"][:3]:
        t = X.norm("".join(p.itertext())).lower()
        if "following clause" in t:
            return "clause"
        if "following provision" in t:
            return "provision"
    return ""

TOPIC_TAGS = {"concept", "task", "reference", "topic"}      # DITA topic-type roots
BODY_TAGS  = {"conbody", "taskbody", "refbody", "body"}      # their bodies

def _is_kind(el, tags, base):
    """Match a DITA element by tag name OR by @class derivation (e.g. base 'topic/body').
    Tag-matching handles FAR files whose elements omit @class (some <concept>/<conbody> do);
    class-matching future-proofs task/reference and any other specialization without enumerating."""
    return el.tag in tags or base in (el.get("class") or "")

def load_topic(path):
    """Return the file's topic-type root element — concept/task/reference/topic (by tag or
    @class derivation from topic/topic) — or None on parse error / no topic element."""
    try:
        raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
        root = ET.fromstring(raw)
    except (ET.ParseError, OSError):
        return None
    return next((el for el in root.iter() if _is_kind(el, TOPIC_TAGS, "topic/topic")), None)

def build_id_map(paths):
    """Global {element @id -> section citation} over a set of DITA files, so an xref to an opaque
    ditaot id ('#SSQFWIXN', '#i1495044') resolves to the citation of the section that element lives
    in — something the structural href parse can't recover (the id carries no number). The citation
    is the file's title autonumber for section files, or 'part X'/'subpart X.Y' for the Part_*/
    Subpart_* container files (whose topic ids are xref targets too). One parse per file; ditaot ids
    are globally unique, so first-wins on the rare collision is safe. Feeds X.set_id_map()."""
    id_map = {}
    for path in paths:
        c = load_topic(path)
        if c is None:
            continue
        b = os.path.basename(path)
        mp = re.match(r"(?i)Part[_-](\d+)(?:[_.]|$)", b)
        ms = re.match(r"(?i)Subpart[_-](\d+)[_.](\w+?)(?:_T48|\.dita$)", b)
        title = c.find("./title")
        num_ph = title.find(".//ph[@props='autonumber']") if title is not None else None
        if mp and not re.match(r"(?i)Section", b):
            cit = "part " + mp.group(1)
        elif ms:
            cit = f"subpart {ms.group(1)}.{ms.group(2)}"
        elif num_ph is not None and X.norm(num_ph.text):
            cit = X.norm(num_ph.text)
        else:
            continue
        for el in c.iter():                        # topic root + every descendant carrying an @id
            eid = el.get("id")
            if eid and eid not in id_map:
                id_map[eid] = cit
    return id_map

def parse_ditamap(path):
    """Parse a FAR-style DITA map. Returns (source_version, files):
      source_version  the <map rev="…"> stamp (e.g. 'FAC 2026-01 March 13, 2026') or '';
      files           ordered, de-duplicated list of .dita hrefs the map references.
    The map references whole files only (never paragraphs), so this is purely the authoritative
    file list + version stamp; intra-file decomposition stays the parser's job. Returns ('', [])
    if the map is missing or unparseable (callers then fall back to a folder scan)."""
    try:
        raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
        root = ET.fromstring(raw)
    except (ET.ParseError, OSError):
        return "", []
    rev = (root.get("rev") or "").strip()
    files, seen = [], set()
    for tr in root.iter("topicref"):
        href = tr.get("href") or ""
        if href.endswith(".dita") and href not in seen:
            seen.add(href); files.append(href)
    return rev, files

def title_text(title_el):
    """The section/clause heading, minus its leading autonumber and trailing period:
    '<ph autonumber>32.503-10</ph> Establishing alternate liquidation rates.' -> 'Establishing
    alternate liquidation rates'. '' when there's no title."""
    if title_el is None:
        return ""
    parts = [title_el.text or ""]
    for ch in title_el:
        if not (ch.tag == "ph" and ch.get("props") == "autonumber"):   # skip the number, keep the words
            parts.append("".join(ch.itertext()))
        parts.append(ch.tail or "")
    t = X.norm("".join(parts))
    return t[:-1].rstrip() if t.endswith(".") else t                   # FAR titles end in a period; drop it

def _file_title(path):
    """The heading of a Part_*/Subpart_* file, minus its 'Part 32 - ' / 'Subpart 32.5 - ' prefix."""
    try:
        raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
        t = next((e for e in ET.fromstring(raw).iter() if e.tag == "title"), None)
    except (ET.ParseError, OSError):
        return ""
    if t is None:
        return ""
    full = re.sub(r"^(?:Part|Subpart)\s+[\d.]+\s*[-–—]\s*", "", X.norm("".join(t.itertext())))
    return full[:-1].rstrip() if full.endswith(".") else full

def hierarchy_titles(input_dir):
    """{part_num: title} and {'part.subpart': title} from the Part_*/Subpart_* files (which the chunker
    otherwise skips). Section/subsection titles come free from the units themselves — see run_chunker."""
    part, subpart = {}, {}
    for p in glob.glob(os.path.join(input_dir, "Part_*.dita")):
        m = re.match(r"Part_(\d+)", os.path.basename(p))
        if m:
            part[m.group(1)] = _file_title(p)
    for p in glob.glob(os.path.join(input_dir, "Subpart_*.dita")):
        m = re.match(r"Subpart_([\d.]+?)\.dita$", os.path.basename(p))
        if m:
            subpart[m.group(1)] = _file_title(p)
    return part, subpart

def decompose(sec_num, tokens, field_levels):
    base = X.components(sec_num)                # part / subpart / section / subsection (bare)
    d = {k: base[k] for k in ("part", "subpart", "section", "subsection")}
    for i, name in enumerate(field_levels):    # paragraph .. bottom
        d[name] = tokens[i] if i < len(tokens) else ""
    return d

def build(path, far, cfg):
    """Return (rows, skip_reason). rows is None when skipped."""
    c = load_topic(path)
    if c is None:
        return None, "no topic (concept/task/reference) / parse error"
    title = c.find("./title")
    num_ph = title.find(".//ph[@props='autonumber']") if title is not None else None
    sec_num = X.norm(num_ph.text) if num_ph is not None else far
    sec_title = title_text(title)              # the heading, shared by every chunk from this file
    conbody = next((el for el in c.iter() if _is_kind(el, BODY_TAGS, "topic/body")), None)
    if conbody is None:
        return None, "no body (conbody/taskbody/refbody/body)"
    url = cfg["url_template"].format(num=sec_num)
    reg = cfg["regulation"]
    bottom = cfg["bottom_depth"]
    field_levels = PARA_LEVELS[:bottom]        # decomposition fields run part..bottom

    # change track: pair each rev-marked span (this PI-free parse, document order) with its
    # [CaseNumber]/[Why] markers (recovered by a PI-preserving parse, same document order).
    rev_meta = changelog.extract_rev_changes(path)
    rev_phs = [el for el in c.iter() if el.get("rev")]
    change_of = {}
    for i, ph in enumerate(rev_phs):
        m = rev_meta[i] if i < len(rev_meta) else {}
        change_of[id(ph)] = {"text": X.norm("".join(ph.itertext())),
                             "fac": ph.get("rev") or m.get("fac", ""),
                             "case_number": m.get("case_number", ""), "why": m.get("why", "")}

    # Instrument-level facts (shared by every row from this file): structural type, terminator, instrument.
    base_type = "subsection" if "-" in sec_num else "section"
    unit_end, end_el, alt_section = find_end_and_alt(conbody)
    instrument = instrument_of(conbody, unit_end)              # clause / provision / '' (regulatory)
    bdate, bpresc = base_meta(conbody) if instrument else ("", "")   # base clause date + prescribing section

    def row(number, typ, tokens, ps, text, scan, exclude=None,
            alternate="", date="", prescribed_by="", end_marker=""):
        exclude = exclude or set()                             # element ids to drop (e.g. the alternate subtree)
        r = {"citation": f"{reg}-{number}", "regulation": reg,
             "source_version": cfg.get("source_version", ""),   # FAR edition (ditamap rev)
             "pipeline_version": cfg.get("pipeline_version", ""),  # producing commit (git short SHA)
             "type": typ,                                       # structural level (FAR 1.105-2)
             "instrument": instrument,                          # functional: clause / provision / ''
             "alternate": alternate,                            # variant: '' (base) or '1'..'5'
             "part_title": "", "subpart_title": "",             # hierarchy breadcrumb: this file's own level is
             "section_title": "", "subsection_title": ""}       # set below; the ancestry is filled in run_chunker
        r.update(decompose(sec_num, tokens, field_levels))
        r["url"] = url
        r["cross_references"] = X.collect_refs(ps, sec_num, url)
        r["external_references"] = X.collect_external_refs(ps)
        imgs = []                                              # tables are inlined as HTML in `text`
        for n in scan:                                         # scan = the element(s) this chunk owns
            for ch in n.iter():
                if ch.tag == "image" and id(ch) not in exclude:
                    iid = X.img_id(ch.get("href") or "")
                    if iid not in imgs:
                        imgs.append(iid)
        r["images"] = imgs
        r["changes"] = [change_of[id(d)] for n in scan for d in n.iter()   # rev-marked spans in this chunk
                        if id(d) in change_of and id(d) not in exclude]
        r["date"] = date                                       # clause/alternate effective date; '' for paragraphs
        r["prescribed_by"] = prescribed_by                     # FAR section prescribing this clause/alternate
        r["reserved"] = X.norm(text).rstrip(".").lower().endswith("[reserved]")
        r["end_marker"] = end_marker                           # clause/provision terminator; base unit only
        r["text"] = text
        return r

    # Alternates + end marker live after the basic clause; pull them out so they don't leak into the
    # unit's text / refs / images / changes (which scan the whole conbody).
    alt_exclude = {id(e) for e in alt_section.iter()} if alt_section is not None else set()
    skip_text = alt_exclude | ({id(end_el)} if end_el is not None else set())
    base_ps = [p for p in conbody.iter("p") if id(p) not in alt_exclude]
    unit_text = X.flatten_section(conbody, url, skip_ids=skip_text)
    rows = [row(sec_num, base_type, [], base_ps, unit_text, [conbody], exclude=alt_exclude,
                date=bdate, prescribed_by=bpresc, end_marker=unit_end)]

    # each alternate becomes its OWN flat row: same citation as the base, distinguished by `alternate`
    # (arabic), inheriting the base's structural type. Its refs/images/changes are scoped to its span.
    for sp in alt_spans(alt_section):
        nodes = sp["nodes"]
        adate, apresc = alt_meta(nodes, url)
        aps = [p for ch in nodes for p in ch.iter("p")]
        rows.append(row(sec_num, base_type, [], aps, "\n".join(X.flatten_nodes(nodes, url)), nodes,
                        alternate=roman_to_arabic(sp["roman"]), date=adate, prescribed_by=apresc))

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
                rows.append(row(cit, level_name(d), nt, list(li.iter("p")), X.flatten_li(li, url), [li]))
            if d < bottom:
                for sub in li.findall("./ol"):
                    walk(sub, nt)

    if bottom >= 1:
        for ol in conbody.findall("./ol"):
            walk(ol, [])

    own = "subsection_title" if "-" in sec_num else "section_title"   # this file's own level (dash => subsection)
    for r in rows:
        r[own] = sec_title
    # supplements have legitimately terse sections ('(b)(2) The DISA GC is the designee.');
    # the canon adapter passes a lower min_text so they aren't dropped as near-empty
    if len(rows) == 1 and len(unit_text) < cfg.get("min_text", MIN_TEXT):
        return None, "near-empty"
    return rows, None

def sort_key(r):
    g = lambda v: int(v) if v.isdigit() else 0
    def rank(t):
        return (ord(t.lower()) - 96) if (len(t) == 1 and t.isalpha()) else (int(t) if t.isdigit() else 0)
    para = [r.get(n, "") for n in PARA_LEVELS]
    return (g(r["part"]), g(r["subpart"]), g(r["section"]), g(r["subsection"]),
            tuple(rank(t) for t in para), r.get("alternate", ""))   # base ('') sorts before its alternates

def _resolve_files(cfg):
    """Return (paths, explicit, missing, source). File set, in priority order:
      * cfg['files'] given    -> use exactly those (source='explicit');
      * a ditamap is present  -> drive the list from the map, authoritative (source='ditamap');
      * otherwise             -> scan input_dir for *.dita (source='folder', the fallback).
    `missing` lists requested/referenced files not found on disk (logged as skips)."""
    files = cfg.get("files")
    if files:
        paths, missing = [], []
        for f in files:
            cands = [f, f + ".dita", os.path.join(cfg["input_dir"], f),
                     os.path.join(cfg["input_dir"], f + ".dita")]
            hit = next((c for c in cands if os.path.isfile(c)), None)
            (paths.append(hit) if hit else missing.append(f))
        return paths, True, missing, "explicit"
    mapname = cfg.get("ditamap")
    mappath = os.path.join(cfg["input_dir"], mapname) if mapname else None
    if mappath and os.path.isfile(mappath):
        _, mapfiles = parse_ditamap(mappath)
        if mapfiles:
            paths, missing = [], []
            for href in mapfiles:
                p = os.path.join(cfg["input_dir"], href)
                (paths.append(p) if os.path.isfile(p) else missing.append(href))
            return paths, False, missing, "ditamap"
    paths = sorted(os.path.join(cfg["input_dir"], f)
                   for f in os.listdir(cfg["input_dir"]) if f.endswith(".dita"))
    return paths, False, [], "folder"

def run_chunker(cfg, progress=False):
    """Resolve the file set (shared by the LLM stage), chunk, return (rows, manifest, sources).
    progress=True prints a heartbeat every 100 files — chunking is otherwise one silent blocking pass,
    which on a slow/networked input_dir can look frozen for minutes."""
    paths, explicit, missing, file_source = _resolve_files(cfg)
    X.set_id_map(build_id_map(paths))          # resolve opaque xref ids to their section citation
    rows, processed, skipped = [], [], []
    sources = {}                               # {unit_citation: .dita file path} — for the raw-file LLM pass
    for f in missing:
        skipped.append({"file": f, "reason": "not found"})
    n = len(paths)
    for i, path in enumerate(paths, 1):
        if progress and (i % 100 == 0 or i == n):
            print(f"  chunked {i}/{n} files…", flush=True)
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
    X.set_id_map({})                           # clear the per-run id map
    rows.sort(key=sort_key)

    # Title breadcrumb: fill each chunk's part/subpart titles (from the Part_*/Subpart_* files) and its
    # parent section_title (harvested from the section units we just chunked; build() already set each
    # chunk's own section_title/subsection_title). Section/subsection titles need the parent to be in the
    # run — true for a full run; a --files subset may leave section_title '' when the parent isn't included.
    part_titles, subpart_titles = hierarchy_titles(cfg["input_dir"])
    sec_of = lambda r: f'{r["part"]}.{r["subpart"]}{r["section"]}'
    section_titles = {sec_of(r): r["section_title"] for r in rows if r["section_title"]}
    for r in rows:
        r["part_title"] = part_titles.get(r["part"], "")
        r["subpart_title"] = subpart_titles.get(f'{r["part"]}.{r["subpart"]}', "")
        if not r["section_title"]:                 # subsection/paragraph rows inherit their section's title
            r["section_title"] = section_titles.get(sec_of(r), "")

    manifest = {"regulation": cfg["regulation"], "input_dir": os.path.abspath(cfg["input_dir"]),
                "file_source": file_source, "bottom_level": cfg["bottom_level"], "files_seen": len(paths),
                "processed_count": len(processed), "skipped_count": len(skipped),
                "processed": processed, "skipped": skipped}
    return rows, manifest, sources
