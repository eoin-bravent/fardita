#!/usr/bin/env python3
"""Text-conservation audit: prove an adapter captured a corpus completely.

The strongest corpus-agnostic completeness check is CONSERVATION OF TEXT: every
substantive segment of the source HTML must either appear in some chunk row, or be
explicitly classified as skipped-by-design (navigation, tables of contents, page
furniture, appendix material the pipeline deliberately excludes). Anything left over is
UNCLASSIFIED RESIDUE -- the alarm metric. A parser that silently drops half a part (the
libxml2 255-depth truncation), mis-detects headings, or skips a file entirely shows up
here as residue, without anyone needing to read the corpus.

Method (era-agnostic -- works on any adapter's output for any regulation):
  1. Chunk pool: canonicalize every row's text + titles into token 8-gram shingles.
  2. For every *.htm(l) file in the edition, split the rendered text into segments
     (visual blocks); a segment is COVERED when >=85%% of its shingles are in the pool
     (canonicalization forgives the cross-generator dash/quote/space artifacts).
  3. Classify uncovered segments by rule (nav / toc / heading / furniture / appendix /
     forms); whatever survives classification is reported with samples.

Usage:
  python corpus_audit.py <edition_dir> --rows rows.json [--regulation AFARS]
                         [--report audit.json] [--min-chars 60]

Exit status 1 when unclassified residue exceeds --fail-pct (default 0.5%% of source).
"""
import os
import re
import sys
import json
import glob
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lxml.html
from chunker.parsers import classify_companion   # regulation-vs-companion seam (Decision A)

# ---------------------------------------------------------------- canonical shingles
_DASHES = {ord(c): "-" for c in "—–‐‑‒―−﹘－­"}


def canon_tokens(s):
    s = (s or "").translate(_DASHES)
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = s.lower()
    return re.findall(r"[a-z0-9]+", s)


def shingles(tokens, k=8):
    if len(tokens) < k:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


# ---------------------------------------------------------------- source segmentation
BLOCK_TAGS = {"p", "div", "li", "dt", "dd", "td", "th", "h1", "h2", "h3", "h4",
              "h5", "h6", "caption", "pre", "blockquote"}


def source_segments(path):
    """Visual text blocks of one HTML file (leaf-ish block elements, document order)."""
    raw = open(path, encoding="utf-8", errors="replace").read()
    raw = re.sub(r"(?i)<a\s+(name\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))\s*>",
                 r"<a \1></a>", raw)                    # legacy unclosed anchors
    raw = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw)        # fromstring rejects XML decls
    try:
        doc = lxml.html.fromstring(raw)
    except Exception:
        return []
    segs = []
    for el in doc.iter():
        if not isinstance(el.tag, str) or el.tag not in BLOCK_TAGS:
            continue
        # leaf-ish: skip containers whose block children will be visited themselves
        if any(isinstance(c.tag, str) and c.tag in BLOCK_TAGS for c in el):
            continue
        t = re.sub(r"\s+", " ", el.text_content()).strip()
        if t:
            segs.append(t)
    return segs


# ---------------------------------------------------------------- residue classifiers
NAV_RX = re.compile(r"(?i)^(«?\s*previous|next\s*»?|toc$|table of contents|top of page"
                    r"|prev\b|home$|parent topic:|related topics?:|related links?:)")
TOC_LINE_RX = re.compile(r"^(Subpart\s+\d|\d{1,4}\.\d+(-\d+)*\s)\S?.{0,120}\.?$")
HEADING_RX = re.compile(r"(?i)^((AFARS|FAR)\s*[-–—]?\s*)?(PART|SUBPART|Appendix)\s")
FURNITURE_RX = re.compile(r"(?i)(revised|revision \d+ dated)\s|^\(?revised|^sec\.$"
                          r"|federal acquisition regulation$|^\*+$|^page \d+|^-+$"
                          r"|created using|generator|copyright")
AUTHORITY_RX = re.compile(r"(?i)^authority:\s+.{0,200}U\.?S\.?C\.?")
# runs of 'NUM Title.' index entries fused into ONE visual block (FARSite leaves TOC
# <li>s unclosed too) -- e.g. '1801.601 General. 1801.602 Contracting Officer
# Responsibilities. 1801.602-2 Delegations ...', optionally led by an ALL-CAPS
# subpart banner. Index furniture, not body prose.
TOC_ENTRY_RX = re.compile(r"[§�]?\d{1,4}\.\d+(?:-\d+)*\s+[^.]{1,140}\.\s*")


TOC_TAIL_RX = re.compile(r"^[§�]?\d{1,4}\.\d+(?:-\d+)*\s+[^.]{0,120}$")


def _looks_toc_run(t):
    hits = TOC_ENTRY_RX.findall(t)
    if not hits:
        return False
    rest = TOC_ENTRY_RX.sub(" ", t).strip()
    rest = TOC_TAIL_RX.sub("", rest).strip()    # final entry cut off mid-title
    rest_toks = canon_tokens(rest)
    if len(hits) >= 2 and len(rest_toks) < 8:
        return True
    return bool(rest) and rest.isupper() and len(rest) < 120


def classify_residue(t, in_toc_zone):
    if NAV_RX.search(t):
        return "nav"
    if FURNITURE_RX.search(t):
        return "furniture"
    if HEADING_RX.match(t):
        return "heading"
    if AUTHORITY_RX.match(t):
        # the CFR statutory-authority editorial note under a part heading; sits before
        # the first section in every render, so no era's parser has a unit to attach
        # it to. Distinct class: visible in residue counts, not an UNCLASSIFIED alarm.
        return "authority-note"
    if t.isupper() and len(t) < 120:         # ALL-CAPS part/subpart title lines
        return "heading"
    if in_toc_zone and TOC_LINE_RX.match(t):
        return "toc"
    if _looks_toc_run(t):
        return "toc"
    if len(canon_tokens(t)) < 8:
        return "short"                       # sub-shingle fragments: labels, dates, cells
    return "UNCLASSIFIED"


def is_content_file(path, skipped_names):
    b = os.path.basename(path).lower()
    if any(s in b for s in skipped_names):
        return False
    return True


DEFAULT_SKIP_FILES = ["toc", "cover", "foreword", "matrix", "index", "correction",
                      "appendix", "appendex", "forms", "preamble", "subchapter",
                      "volume", "lsatable", "mapfile", "afartoc", "farmtoc",
                      "loading", "search", "notes"]
# leading § (or its U+FFFD utf-8/replace mojibake) before the number: NRCAR-style
# FARSite heading lines ('§2001.603 Selection, ...')
SEC_LINE_RX = re.compile(r"^[§�]?\s*\d{1,4}\.\d+(-\d+)*\s+\S")
SEC_NUM_STRIP_RX = re.compile(r"^[§�]?\s*\d{1,4}\.\d+(-\d+)*\s+")
# a heading FUSED to its section body in one visual block -- FARSite mirrors leave the
# heading <p> unclosed, so 'NUM Title. body ...' flattens as a single segment even
# though the parser correctly split it into a title + body rows
FUSED_HEAD_RX = re.compile(r"^[§�]?\s*\d{1,4}\.\d+(?:-\d+)*\s+(?P<title>[^.]{1,200}\.)"
                           r"\s+(?P<body>.{20,})$", re.S)


# Which classified-away (skip-by-design) segments deserve an LLM second opinion. A
# misclassification is dangerous only when REAL regulation text was skipped, and real
# text is long, prose-like -- so length is the signal, not the class. Always suspect the
# aggressive whole-chunk classes; suspect the structural ones (heading/toc) only when the
# segment is too long to plausibly be furniture. nav/furniture/short are left alone.
# always-suspect: whole-file / whole-section droppers (a misclass loses a lot)
SUSPECT_CLASSES = {"companion-doc", "annex"}
# suspect only when TOO LONG to be furniture (a short one really is a heading/TOC line/
# authority note; a long one may be real regulation text wrongly bucketed)
SUSPECT_LONG_CLASSES = {"heading", "toc", "toc-foreign", "authority-note"}
SUSPECT_LONG_CHARS = 200
SUSPECT_CAP_PER_CLASS = 8


def is_suspect_skip(cls, text):
    """True if a skip-by-design segment is substantive enough to warrant LLM validation
    (did the parser wrongly drop real regulation text under this class?)."""
    if cls in SUSPECT_CLASSES:
        return True
    return cls in SUSPECT_LONG_CLASSES and len(text) > SUSPECT_LONG_CHARS


def section_completeness(edition_dir, rows, era):
    """INDEPENDENT, classifier-free completeness check for ANY HTML era. The publisher's
    own section splitter (archive_adapter.declared_sections) yields every section it
    declared with substantive body text; each MUST produce a row. `missing` = declared,
    body-bearing sections that produced NO row -- a PROVABLE drop, owing nothing to the
    conservation shingle-pool or any skip-by-design classifier, so it catches losses the
    accounted% metric can mask (a dropped paragraph whose text shingle-matches another
    row). Empty parent headers (e.g. 201.105, whose text lives in 201.105-1/-2) are not
    counted. Returns {publisher_with_body, parsed, missing:[...]}."""
    from chunker.parsers import _adapter as A
    real = A.declared_sections(edition_dir, era)
    parsed = {r["citation"].split("-", 1)[-1].split("(")[0] for r in rows}
    return {"publisher_with_body": len(real), "parsed": len(parsed),
            "missing": sorted(real - parsed)}


# back-compat alias (DITA-family callers)
def dita_section_completeness(edition_dir, rows):
    return section_completeness(edition_dir, rows, "ditaot-topics")


def audit(edition_dir, rows, min_chars=60, k=8, cover_frac=0.85,
          skip_files=DEFAULT_SKIP_FILES, files_rx=None, companion_rx=None,
          source_files=None, companion_filter=None, companion_rx_mode="residue"):
    """files_rx: optional regex on basenames restricting which files count as source --
    for edition folders that ship MULTIPLE renders of the same content (AFARS agov
    folders carry both a pandoc and a site render), audit the render the parser reads.

    source_files: optional explicit list of absolute paths to treat as THE source --
    stronger than files_rx (which can only filter by basename, useless when the
    duplicate renders share basenames across sibling dirs, as the ditaot-topics
    copypaste-AllTopic/FullParts/Subparts trees do). When given, exactly these files
    are audited; everything else on disk is ignored. Pass the SAME list the parser
    read so duplicate renders don't masquerade as unaccounted source.

    companion_rx: optional regex on basenames marking COMPANION DOCUMENT files that are
    a different document class from the regulation itself (DAFFARS MP/IG mandatory
    procedures, SOFARS form attachments) -- their text is counted under the visible
    'companion-doc' residue class instead of UNCLASSIFIED, because the regulation
    parser is not SUPPOSED to produce rows for them (they want their own store, like
    DFARS PGI). They still appear in per-file counts, so nothing is hidden."""
    # 1) chunk pool (+ a fine-grained title pool: headings/TOC lines are 'NUM Title.'
    #    whose title text the rows carry in their title fields, without the number)
    pool = set()
    title_pool = set()
    titles_by_num = {}                # '1808.405-3' -> its parsed title (fused headings)
    for r in rows:
        toks = canon_tokens(" ".join([r.get("text", ""), r.get("part_title", ""),
                                      r.get("subpart_title", ""), r.get("section_title", ""),
                                      r.get("subsection_title", "")]))
        pool |= shingles(toks, k)
        for f in ("part_title", "subpart_title", "section_title", "subsection_title"):
            tt = canon_tokens(r.get(f, "") or "")
            if tt:
                title_pool |= shingles(tt, 3)
        cit = (r.get("citation") or "").split("-", 1)[-1].split("(")[0]
        own = r.get("subsection_title") or r.get("section_title")
        if cit and own:
            titles_by_num.setdefault(cit, own)

    if source_files is not None:
        files = sorted(source_files)
    else:
        files = sorted(f for f in glob.glob(os.path.join(edition_dir, "**", "*.htm*"),
                                            recursive=True) if "__MACOSX" not in f)
    report = {"files": [], "totals": {"source_chars": 0, "covered_chars": 0,
                                      "residue": {}, "skipped_files": 0}}
    samples = []
    class_samples = {}                       # cls -> [{file,text}], capped, for LLM check

    def record_skip(cls, text, f):
        """Stash a substantive skip-by-design segment for LLM classifier-validation."""
        if not is_suspect_skip(cls, text):
            return
        bucket = class_samples.setdefault(cls, [])
        if len(bucket) < SUSPECT_CAP_PER_CLASS:
            bucket.append({"file": os.path.relpath(f, edition_dir), "text": text[:400]})

    frx = re.compile(files_rx) if files_rx else None
    crx = re.compile(companion_rx) if companion_rx else None
    for f in files:
        if not is_content_file(f, skip_files) or \
                (frx and not frx.search(os.path.basename(f))):
            report["totals"]["skipped_files"] += 1
            continue
        # companion_filter (Decision A): 'exclude' drops companion files from the regulation
        # denominator entirely (honest reg-only covered%); 'only' keeps ONLY companion files
        # (the companion certificate). Same classifier as the capture seam.
        if companion_filter is not None:
            _dc = classify_companion(os.path.basename(f))
            if (companion_filter == "exclude" and _dc) or \
               (companion_filter == "only" and not _dc):
                report["totals"]["skipped_files"] += 1
                continue
        if crx and crx.search(os.path.basename(f)):
            if companion_rx_mode == "exclude":       # honest reg-only: drop from denominator
                report["totals"]["skipped_files"] += 1
                continue
            csegs = source_segments(f)
            chars = sum(len(t) for t in csegs)
            report["files"].append({"file": os.path.relpath(f, edition_dir),
                                    "source_chars": chars, "covered_chars": 0,
                                    "residue": {"companion-doc": chars}})
            report["totals"]["source_chars"] += chars
            report["totals"]["residue"]["companion-doc"] = \
                report["totals"]["residue"].get("companion-doc", 0) + chars
            for t in csegs:                  # LLM spot-check: is this really a companion?
                if len(t) >= min_chars:
                    record_skip("companion-doc", t, f)
            continue
        segs = source_segments(f)
        n_src = n_cov = 0
        residue = {}
        first_body_seen = False
        for t in segs:
            n_src += len(t)
            toks = canon_tokens(t)
            sh = shingles(toks, k)
            if sh:
                hit = len(sh & pool) / len(sh)
            else:
                hit = 1.0
            if hit >= cover_frac:
                n_cov += len(t)
                if len(toks) >= 12:
                    first_body_seen = True
                continue
            # 'NUM Title.' lines (a section's own heading, or a TOC entry for it):
            # covered when the title text -- which rows carry WITHOUT the number --
            # matches the title pool; otherwise it's a TOC entry for a section this
            # corpus doesn't contain (supplements list the PARENT regulation's headings
            # for context). Never body prose, so it gets its own visible class --
            # a genuinely dropped section still alarms via its uncovered PARAGRAPHS.
            if SEC_LINE_RX.match(t) and len(toks) <= 30:
                ttoks = canon_tokens(SEC_NUM_STRIP_RX.sub("", t).rstrip("."))
                tsh = shingles(ttoks, 3)
                if tsh and len(tsh & title_pool) / len(tsh) >= 0.6:
                    n_cov += len(t)
                else:
                    residue["toc-foreign"] = residue.get("toc-foreign", 0) + len(t)
                    record_skip("toc-foreign", t, f)
                continue
            # heading fused to its body in one block (unclosed FARSite heading <p>):
            # covered when the TITLE matches the title pool and the BODY alone clears
            # the shingle bar -- the heading/body boundary shingles can never be in the
            # pool, which is exactly what dragged these below cover_frac. A section
            # whose body the parser actually dropped still fails the body test.
            covered_fused = False
            m_num = SEC_NUM_STRIP_RX.match(t)
            if m_num:
                # exact split: the parser's own title for this number, matched as a
                # case-insensitive prefix of the remainder
                num = re.search(r"\d{1,4}\.\d+(?:-\d+)*", m_num.group(0)).group(0)
                known_t = titles_by_num.get(num, "")
                remainder = t[m_num.end():]
                if known_t and remainder.lower().startswith(known_t.lower()):
                    body = remainder[len(known_t):].lstrip(" .–—-")
                    btoks = canon_tokens(body)
                    bsh = shingles(btoks, k)
                    bhit = (len(bsh & pool) / len(bsh)) if bsh else 1.0
                    covered_fused = bhit >= cover_frac
            if not covered_fused:
                m_fused = FUSED_HEAD_RX.match(t)
                if m_fused:
                    btoks = canon_tokens(m_fused.group("body"))
                    bsh = shingles(btoks, k)
                    bhit = (len(bsh & pool) / len(bsh)) if bsh else 0.0
                    ttoks = canon_tokens(m_fused.group("title").rstrip("."))
                    tsh = shingles(ttoks, 3)
                    t_ok = (tsh and len(tsh & title_pool) / len(tsh) >= 0.6) \
                        or (not tsh and ttoks)
                    covered_fused = bhit >= cover_frac and t_ok
            if covered_fused:
                n_cov += len(t)
                first_body_seen = True
                continue
            # 'REG Part NNNN Title' heading lines (FARSite et al.): judge by the TITLE
            # tokens alone -- the reg-name/number prefix would dilute the shingle ratio
            m_part = re.match(r"(?i)^(?:[A-Z]{2,10}\s+)?part\s+\d{1,4}\s*[-–—:]?\s+(.{3,140})$", t)
            if m_part and len(toks) <= 30:
                ttoks = canon_tokens(m_part.group(1).rstrip("."))
                tsh = shingles(ttoks, 3)
                if (tsh and len(tsh & title_pool) / len(tsh) >= 0.6) or \
                   (not tsh and ttoks):          # 1-2-word titles: too short to shingle
                    n_cov += len(t)
                else:
                    residue["toc-foreign"] = residue.get("toc-foreign", 0) + len(t)
                    record_skip("toc-foreign", t, f)
                continue
            cls = classify_residue(t, in_toc_zone=not first_body_seen)
            residue[cls] = residue.get(cls, 0) + len(t)
            if cls == "UNCLASSIFIED" and len(t) >= min_chars and len(samples) < 40:
                samples.append({"file": os.path.relpath(f, edition_dir),
                                "text": t[:220]})
            else:
                record_skip(cls, t, f)          # substantive skip -> LLM second opinion
        report["files"].append({"file": os.path.relpath(f, edition_dir),
                                "source_chars": n_src, "covered_chars": n_cov,
                                "residue": residue})
        report["totals"]["source_chars"] += n_src
        report["totals"]["covered_chars"] += n_cov
        for kk, v in residue.items():
            report["totals"]["residue"][kk] = report["totals"]["residue"].get(kk, 0) + v
    report["unclassified_samples"] = samples
    # substantive skip-by-design segments, flattened, for LLM classifier-validation
    report["classified_samples"] = [dict(cls=c, **s)
                                    for c, ss in class_samples.items() for s in ss]
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edition_dir")
    ap.add_argument("--rows", required=True, help="the adapter's chunked rows for this edition")
    ap.add_argument("--report", default="")
    ap.add_argument("--min-chars", type=int, default=60)
    ap.add_argument("--fail-pct", type=float, default=0.5,
                    help="fail when UNCLASSIFIED residue exceeds this %% of source text")
    ap.add_argument("--skip-files", default=",".join(DEFAULT_SKIP_FILES),
                    help="comma list of filename substrings excluded by design")
    args = ap.parse_args()

    rows = json.load(open(args.rows, encoding="utf-8"))
    rep = audit(args.edition_dir, rows, min_chars=args.min_chars,
                skip_files=[s for s in args.skip_files.split(",") if s])
    tot = rep["totals"]
    src, cov = tot["source_chars"], tot["covered_chars"]
    res = tot["residue"]
    uncls = res.get("UNCLASSIFIED", 0)
    print(f"source text: {src:,} chars in {len(rep['files'])} content files "
          f"({tot['skipped_files']} files excluded by design)")
    print(f"covered by chunks: {cov:,} ({100*cov/max(src,1):.2f}%)")
    print(f"residue by class: " + ", ".join(f"{k}={v:,}" for k, v in sorted(res.items())))
    print(f"UNCLASSIFIED residue: {uncls:,} chars = {100*uncls/max(src,1):.3f}% "
          f"(fail threshold {args.fail_pct}%)")
    worst = sorted(rep["files"], key=lambda x: -(x["residue"].get("UNCLASSIFIED", 0)))[:5]
    for w in worst:
        u = w["residue"].get("UNCLASSIFIED", 0)
        if u:
            print(f"  worst: {w['file']}  unclassified={u:,}")
    for s in rep["unclassified_samples"][:6]:
        print(f"  sample [{s['file']}]: {s['text'][:130]!r}")
    if args.report:
        json.dump(rep, open(args.report, "w", encoding="utf-8"), indent=1,
                  ensure_ascii=False)
        print(f"report -> {args.report}")
    ok = 100 * uncls / max(src, 1) <= args.fail_pct
    print("VERDICT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
