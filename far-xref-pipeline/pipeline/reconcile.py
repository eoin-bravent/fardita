#!/usr/bin/env python3
"""Stage 3: reconcile deterministic refs (parser) vs the blind LLM audit.

Every reference is ATOMIC (ranges are pre-expanded upstream), so reconciliation is a
symmetric set comparison per unit. We build one MASTER LIST (ledger) of every atomic
target, each tagged by who found it:

  * corroborated     -> parser AND llm found it          (auto-accept)
  * parser_explicit  -> parser found via <xref>, llm did not   (auto-accept; markup authoritative)
  * parser_inferred  -> parser found via prose/range, llm did not   (REVIEW: lower confidence)
  * llm_only         -> llm found it, parser missed it          (REVIEW: the high-value catch)

needs_review = parser_inferred | llm_only — only these go to the human queue and the LLM judge.
The full ledger (all four statuses) drives the review page so agreements are inspectable too.
"""
import re

CIT = re.compile(r"^(\d+\.\d+(-\d+)?)((\([A-Za-z0-9]+\))*)$")   # 5.202 / 6.302-2 / 5.202(a)(2)
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
    m = re.match(r"(\d+\.\d+(?:-\d+)?)", c or "")    # leading citation number
    return m.group(1) if m else (c or "")

def cr_context(cr):
    """Representative evidence string from a cross_reference (first mention)."""
    ms = cr.get("mentions")
    return ms[0].get("context", "") if ms else cr.get("context", "")

def grammar_ok(c):
    return bool(CIT.match(c) or SUBPART.match(c) or PART.match(c))

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

def reconcile(rows, llm_by_cit, addr_map):
    """rows: chunk rows. llm_by_cit: {unit_citation: [ {target, evidence} ]}.
    Returns (ledger, stats). Only unit-level rows (section/subsection) are reconciled."""
    ledger = []
    stats = {"corroborated": 0, "parser_explicit": 0, "parser_inferred": 0, "llm_only": 0}
    units = [r for r in rows if r["type"] in ("section", "subsection")]
    for u in units:
        cit = u["citation"]
        self_cit = norm_cit(strip_agency(cit))             # exact self-reference (e.g. 5.101 -> 5.101 / "this section")
        parser_map = {}                                    # norm target -> {kind, evidence}
        for cr in u["cross_references"]:
            t = norm_cit(cr["target"])
            if t and t != self_cit and t not in parser_map:
                parser_map[t] = {"kind": cr.get("confidence", "inferred"), "evidence": cr_context(cr)}
        llm_map = {}                                       # norm target -> {evidence, validation}
        for ref in llm_by_cit.get(cit, []):
            raw = ref.get("target", "")
            if not raw:
                continue
            t, status = validate(raw, addr_map)
            if t and t != self_cit and t not in llm_map:
                llm_map[t] = {"evidence": ref.get("evidence", ""), "validation": status}
        for t in sorted(set(parser_map) | set(llm_map)):
            p, l = parser_map.get(t), llm_map.get(t)
            if p and l:
                status = "corroborated"
            elif p:
                status = "parser_explicit" if p["kind"] == "explicit" else "parser_inferred"
            else:
                status = "llm_only"
            stats[status] += 1
            ledger.append({
                "unit": cit, "url": u["url"], "target": t, "status": status,
                "validation": l["validation"] if l else validate(t, addr_map)[1],
                "parser": {"kind": p["kind"], "evidence": p["evidence"]} if p else None,
                "llm": {"evidence": l["evidence"]} if l else None,
                "judge": None,                             # filled by the optional judge stage
                "needs_review": status in ("parser_inferred", "llm_only"),
            })
    return ledger, stats
