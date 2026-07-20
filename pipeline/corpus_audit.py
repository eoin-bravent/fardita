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
                    r"|prev\b|home$)")
TOC_LINE_RX = re.compile(r"^(Subpart\s+\d|\d{1,4}\.\d+(-\d+)*\s)\S?.{0,120}\.?$")
HEADING_RX = re.compile(r"(?i)^((AFARS|FAR)\s*[-–—]?\s*)?(PART|SUBPART|Appendix)\s")
FURNITURE_RX = re.compile(r"(?i)(revised|revision \d+ dated)\s|^\(?revised|^sec\.$"
                          r"|federal acquisition regulation$|^\*+$|^page \d+|^-+$"
                          r"|created using|generator|copyright")


def classify_residue(t, in_toc_zone):
    if NAV_RX.search(t):
        return "nav"
    if FURNITURE_RX.search(t):
        return "furniture"
    if HEADING_RX.match(t):
        return "heading"
    if t.isupper() and len(t) < 120:         # ALL-CAPS part/subpart title lines
        return "heading"
    if in_toc_zone and TOC_LINE_RX.match(t):
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
SEC_LINE_RX = re.compile(r"^\d{1,4}\.\d+(-\d+)*\s+\S")


def audit(edition_dir, rows, min_chars=60, k=8, cover_frac=0.85,
          skip_files=DEFAULT_SKIP_FILES, files_rx=None):
    """files_rx: optional regex on basenames restricting which files count as source --
    for edition folders that ship MULTIPLE renders of the same content (AFARS agov
    folders carry both a pandoc and a site render), audit the render the parser reads."""
    # 1) chunk pool (+ a fine-grained title pool: headings/TOC lines are 'NUM Title.'
    #    whose title text the rows carry in their title fields, without the number)
    pool = set()
    title_pool = set()
    for r in rows:
        toks = canon_tokens(" ".join([r.get("text", ""), r.get("part_title", ""),
                                      r.get("subpart_title", ""), r.get("section_title", ""),
                                      r.get("subsection_title", "")]))
        pool |= shingles(toks, k)
        for f in ("part_title", "subpart_title", "section_title", "subsection_title"):
            tt = canon_tokens(r.get(f, "") or "")
            if tt:
                title_pool |= shingles(tt, 3)

    files = sorted(f for f in glob.glob(os.path.join(edition_dir, "**", "*.htm*"),
                                        recursive=True) if "__MACOSX" not in f)
    report = {"files": [], "totals": {"source_chars": 0, "covered_chars": 0,
                                      "residue": {}, "skipped_files": 0}}
    samples = []
    frx = re.compile(files_rx) if files_rx else None
    for f in files:
        if not is_content_file(f, skip_files) or \
                (frx and not frx.search(os.path.basename(f))):
            report["totals"]["skipped_files"] += 1
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
                ttoks = canon_tokens(re.sub(r"^\d{1,4}\.\d+(-\d+)*\s+", "", t).rstrip("."))
                tsh = shingles(ttoks, 3)
                if tsh and len(tsh & title_pool) / len(tsh) >= 0.6:
                    n_cov += len(t)
                else:
                    residue["toc-foreign"] = residue.get("toc-foreign", 0) + len(t)
                continue
            cls = classify_residue(t, in_toc_zone=not first_body_seen)
            residue[cls] = residue.get(cls, 0) + len(t)
            if cls == "UNCLASSIFIED" and len(t) >= min_chars and len(samples) < 40:
                samples.append({"file": os.path.relpath(f, edition_dir),
                                "text": t[:220]})
        report["files"].append({"file": os.path.relpath(f, edition_dir),
                                "source_chars": n_src, "covered_chars": n_cov,
                                "residue": residue})
        report["totals"]["source_chars"] += n_src
        report["totals"]["covered_chars"] += n_cov
        for kk, v in residue.items():
            report["totals"]["residue"][kk] = report["totals"]["residue"].get(kk, 0) + v
    report["unclassified_samples"] = samples
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
