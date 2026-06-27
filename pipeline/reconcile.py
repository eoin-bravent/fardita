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
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract_json as X                          # parse_external (canonicalize LLM external citations)

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

ALT_RE = re.compile(r"\s*Alternate\s*([IVX]+)\s*$", re.I)
def split_alternate(target):
    """Pull a trailing 'Alternate <roman>' qualifier off a citation -> (citation, alternate).
    A FAR Alternate (I/II/…) is a clause *variant*: the cross-reference edge target is the BASE
    clause, the alternate qualifies it. Handles 'X Alternate I', 'X AlternateI', and the
    space-stripped 'XAlternateI'; alternate is '' when none. This keeps an alternate reference from
    becoming an invalid mangled citation, and lets it corroborate the base-clause edge."""
    s = (target or "").strip()
    m = ALT_RE.search(s)
    return (s[:m.start()].rstrip(), m.group(1).upper()) if m else (s, "")

def section_root(c):
    m = re.match(r"(\d+\.\d+(?:-\d+)?)", c or "")    # leading citation number
    return m.group(1) if m else (c or "")

# FAR paren ladder by depth (verified across the corpus): (a)(1)(i)(A)(1)(i)…
_LADDER = ["alpha", "digit", "roman", "alpha", "digit", "roman"]
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

def _roman_int(s):
    s = s.lower()
    if not s or any(c not in _ROMAN for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        v = _ROMAN[c]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total

def cit_sort_key(target):
    """Natural FAR order: 5.202(a)(1) < 5.202(a)(4) < 5.202(a)(11) < 5.202(b); romans by value."""
    m = re.match(r"(\d+)\.(\d+)(?:-(\d+))?(.*)$", target or "")
    if not m:                                          # subpart/part/other -> after numeric, by its numbers
        nums = [int(x) for x in re.findall(r"\d+", target or "")]
        return (1, nums or [0], target or "")
    key = [int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)]
    for i, tk in enumerate(re.findall(r"\(([A-Za-z0-9]+)\)", m.group(4))):
        typ = _LADDER[i] if i < len(_LADDER) else "alpha"
        if typ == "digit":
            key.append((0, int(tk)) if tk.isdigit() else (2, tk.lower()))
        elif typ == "roman":
            v = _roman_int(tk)
            key.append((0, v) if v is not None else (2, tk.lower()))
        else:
            key.append((1, tk.lower()))
    return (0, key)

def cr_evidence(cr):
    """Representative evidence string from a cross_reference (first mention)."""
    ms = cr.get("mentions")
    return ms[0].get("evidence", "") if ms else cr.get("evidence", "")

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

def auto_decisions(ledger, judge_on):
    """Build a decisions list (same shape the review page emits) for hands-off `--auto-accept` runs,
    so the human queue can be skipped. Two policies:

      judge_on=False  UNION: accept everything either method found. Parser refs (incl. parser_inferred)
                      are already kept by `apply`, so we only need to accept the `llm_only` catches.
      judge_on=True   JUDGE: for the internal disagreements the judge ruled on, mirror its verdict
                      (accept / reject / manual). The judge never sees external refs, and may leave an
                      item unjudged — those fall back to the union default (accept what was found).

    Every emitted decision is tagged by='auto' so `apply` records status `auto_accepted` (still auditable)."""
    decs = []
    for it in ledger:
        if not it.get("needs_review"):                     # corroborated / parser_explicit already trusted
            continue
        base = {"unit": it["unit"], "scope": it.get("scope", "internal"), "target": it["target"],
                "locator": it.get("locator", ""), "status": it["status"], "by": "auto"}
        j = it.get("judge") or {}
        choice = j.get("choice") if judge_on else None
        if choice in ("accept", "reject"):
            decs.append({**base, "choice": choice})
        elif choice == "manual":
            decs.append({**base, "choice": "manual", "value": j.get("value", [])})
        elif it["status"] == "llm_only":                   # union default (also: judge-on items the judge skipped)
            decs.append({**base, "choice": "accept"})
        # parser_inferred with no verdict: kept by apply as parser_only — no decision needed
    return decs

def reconcile(rows, llm_by_cit, addr_map):
    """rows: chunk rows. llm_by_cit: {unit_citation: [ {target, evidence, scope, ref_type} ]}.
    Returns (ledger, stats). Internal + external refs reconciled per unit; items tagged `scope`."""
    ledger = []
    stats = {"corroborated": 0, "parser_explicit": 0, "parser_inferred": 0, "llm_only": 0}
    units = [r for r in rows if r["type"] in ("section", "subsection")]
    for u in units:
        cit = u["citation"]
        self_cit = norm_cit(strip_agency(cit))             # exact self-reference (e.g. 5.101 -> 5.101 / "this section")

        # ----- internal references (FAR -> FAR) -----
        parser_map = {}                                    # base norm target -> {kind, evidence, alternate}
        for cr in u["cross_references"]:
            base, alt = split_alternate(cr["target"])      # 'X Alternate I' -> ('X','I'); edge keys on base X
            t = norm_cit(base)
            if t and t != self_cit:
                e = parser_map.setdefault(t, {"kind": cr.get("confidence", "inferred"),
                                              "evidence": cr_evidence(cr), "alternate": ""})
                if alt and not e["alternate"]:
                    e["alternate"] = alt
        llm_map = {}                                       # base norm target -> {evidence, validation, alternate}
        for ref in llm_by_cit.get(cit, []):
            if ref.get("scope") == "external":
                continue
            raw = ref.get("target", "")
            if not raw:
                continue
            base, alt = split_alternate(raw)               # an alternate ref validates as its base clause
            t, status = validate(base, addr_map)
            if t and t != self_cit:
                e = llm_map.setdefault(t, {"evidence": ref.get("evidence", ""),
                                           "validation": status, "alternate": ""})
                if alt and not e["alternate"]:
                    e["alternate"] = alt
        for t in sorted(set(parser_map) | set(llm_map), key=cit_sort_key):
            p, l = parser_map.get(t), llm_map.get(t)
            if p and l:
                status = "corroborated"
            elif p:
                status = "parser_explicit" if p["kind"] == "explicit" else "parser_inferred"
            else:
                status = "llm_only"
            stats[status] += 1
            ledger.append({
                "unit": cit, "url": u["url"], "scope": "internal", "target": t, "status": status,
                "alternate": (l and l["alternate"]) or (p and p["alternate"]) or "",  # FAR clause variant (I/II/…)
                "validation": l["validation"] if l else validate(t, addr_map)[1],
                "parser": {"kind": p["kind"], "evidence": p["evidence"]} if p else None,
                "llm": {"evidence": l["evidence"]} if l else None,
                "judge": None,                             # filled by the optional judge stage
                "needs_review": status in ("parser_inferred", "llm_only"),
            })

        # ----- external references (FAR -> other government documents) -----
        p_ext = {}                                         # (target, locator) -> {ref_type, citation, label, levels, evidence}
        for cr in u.get("external_references", []):
            key = (cr["target"], cr.get("locator", ""))
            if key not in p_ext:
                p_ext[key] = {"ref_type": cr["ref_type"], "citation": cr["citation"],
                              "node_label": cr.get("node_label", cr["target"]), "href": cr.get("href", ""),
                              "division_levels": cr.get("division_levels", []), "evidence": cr_evidence(cr)}
        l_ext = {}
        for ref in llm_by_cit.get(cit, []):
            if ref.get("scope") != "external":
                continue
            raw = ref.get("target", "")
            if not raw:
                continue
            parsed = X.parse_external(raw)
            if not parsed:                                 # not a rigid type (USC/CFR/EO/Pub.L./OMB) -> drop the noise
                continue
            key = (parsed["target"], parsed["locator"])
            if key not in l_ext:
                l_ext[key] = {"ref_type": parsed["ref_type"], "citation": parsed["citation"],
                              "node_label": parsed["node_label"], "division_levels": parsed["division_levels"],
                              "evidence": ref.get("evidence", "")}
        for key in sorted(set(p_ext) | set(l_ext)):
            tgt, loc = key
            p, l = p_ext.get(key), l_ext.get(key)
            status = "corroborated" if (p and l) else ("parser_explicit" if p else "llm_only")
            stats[status] += 1
            src = p or l
            ledger.append({
                "unit": cit, "url": u["url"], "scope": "external",
                "target": tgt, "locator": loc, "ref_type": src["ref_type"],
                "node_label": src.get("node_label", tgt), "href": src.get("href", ""),
                "citation": src["citation"], "division_levels": src.get("division_levels", []),
                "status": status, "validation": "external",
                "parser": {"kind": "explicit", "evidence": p["evidence"]} if p else None,
                "llm": {"evidence": l["evidence"]} if l else None,
                "judge": None,
                "needs_review": status == "llm_only",
            })
    return ledger, stats
