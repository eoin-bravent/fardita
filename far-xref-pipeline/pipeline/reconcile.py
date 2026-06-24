#!/usr/bin/env python3
"""Stage 3: reconcile deterministic refs (parser) vs the blind LLM audit.

Policy (locked in discussion):
  * corroborated (parser target == LLM target) -> auto-accept, never queued.
  * det-only (parser found from <xref>, LLM didn't) -> kept; markup is authoritative.
  * llm-new (LLM found, parser missed) -> ALWAYS human review (no auto-accept of new).
  * conflict (same phrase, different resolved target) -> human review.
The LLM never overrides; it only corroborates or raises a flag.
"""
import re

CIT = re.compile(r"^(\d+\.\d+(-\d+)?)((\([A-Za-z0-9]+\))*)$")   # 5.202 / 6.302-2 / 5.202(a)(2)
RANGE = re.compile(r"^\d+\.\d+(-\d+)?\([A-Za-z0-9]+\)-\([A-Za-z0-9]+\)$")   # 5.203(a)-(d)
SUBPART = re.compile(r"^subpart\s+\d+\.\d+$", re.I)
PART = re.compile(r"^part\s+\d+$", re.I)

def norm_cit(s):
    s = " ".join((s or "").strip().split())
    if s.lower().startswith("subpart"):
        return "subpart " + s.split()[-1]
    if s.lower().startswith("part"):
        return "part " + s.split()[-1]
    return s.replace(" ", "")

def section_root(c):
    m = re.match(r"(\d+\.\d+(?:-\d+)?)", c or "")    # leading citation number (works for ranges too)
    return m.group(1) if m else (c or "")

def cr_context(cr):
    """Representative evidence string from a cross_reference (first mention)."""
    ms = cr.get("mentions")
    return ms[0].get("context", "") if ms else cr.get("context", "")

def grammar_ok(c):
    return bool(CIT.match(c) or RANGE.match(c) or SUBPART.match(c) or PART.match(c))

def validate(target, addr_map):
    t = norm_cit(target)
    if not grammar_ok(t):
        return t, "invalid"                       # not a regulation citation (likely external / hallucination)
    if t in addr_map or section_root(t) in addr_map:
        return t, "resolves"
    return t, "not_loaded"                         # real-looking citation we didn't chunk

AGENCY_PREFIX = re.compile(r"^[A-Za-z]+-(?=\d)")    # 'FAR-5.101' -> '5.101' (leaves '6.302-2', 'subpart 25.4')

def strip_agency(c):
    return AGENCY_PREFIX.sub("", c or "")

def build_address_map(rows):
    m = set()
    for r in rows:
        bare = norm_cit(strip_agency(r["citation"]))   # targets are bare; match against bare citations
        m.add(bare)
        m.add(section_root(bare))
    return m

def det_targets(unit_row):
    """All resolved targets the parser found in this unit."""
    return [(norm_cit(cr["target"]), cr) for cr in unit_row["cross_references"]]

def reconcile(rows, llm_by_cit, addr_map):
    """rows: chunk rows. llm_by_cit: {unit_citation: [ {target, evidence} ]}.
    Returns (queue, stats, confirmed). Only unit-level rows (section/subsection) are reconciled."""
    queue, stats = [], {"corroborated": 0, "det_only": 0, "llm_new": 0, "conflict": 0}
    confirmed = {}                                    # {unit: [corroborated targets]} for provenance
    units = [r for r in rows if r["type"] in ("section", "subsection")]
    for u in units:
        cit = u["citation"]
        det = det_targets(u)
        det_set = {t for t, _ in det}
        det_ctx = {t: cr for t, cr in det}
        det_roots = {section_root(t) for t in det_set}

        for ref in llm_by_cit.get(cit, []):
            raw = ref.get("target", "")
            if not raw:
                continue
            t, status = validate(raw, addr_map)
            if t in det_set:
                stats["corroborated"] += 1
                confirmed.setdefault(cit, [])
                if t not in confirmed[cit]:
                    confirmed[cit].append(t)
                continue
            # not in parser's set: llm-new, unless it shares a section root -> conflict
            bucket = "conflict" if section_root(t) in det_roots else "llm_new"
            stats[bucket] += 1
            parser_sugg = None
            if bucket == "conflict":
                pr = next((d for d in det_set if section_root(d) == section_root(t)), None)
                parser_sugg = {"target": pr, "evidence": cr_context(det_ctx[pr])} if pr else None
            queue.append({
                "unit": cit, "url": u["url"], "bucket": bucket, "validation": status,
                "parser": parser_sugg,                       # None for pure llm_new
                "llm": {"target": t, "evidence": ref.get("evidence", "")},
                "judge": None,                               # filled by the optional judge stage
                "decision": "pending",
            })
        # det-only = parser targets the LLM never produced (kept, not queued)
        llm_set = {validate(ref.get("target", ""), addr_map)[0]
                   for ref in llm_by_cit.get(cit, []) if ref.get("target")}
        stats["det_only"] += len(det_set - llm_set)
    return queue, stats, confirmed
