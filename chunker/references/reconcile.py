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

Ported verbatim from pipeline/reconcile.py; only the import is rewired to the chunker package.
The FAR-shaped citation grammar (CIT/norm_cit/section_root/cit_sort_key) is preserved as-is —
supplements mirror FAR numbering, so it holds broadly; per-agency generalization + cross-
regulation targets are handled in R4 (extract/grammar), not here.
"""
import re
from chunker import extract_json as X               # parse_external (canonicalize LLM external citations)

CIT = re.compile(r"^(\d+\.\d+(-\d+)*)((\([A-Za-z0-9]+\))*)$")   # 5.202 / 6.302-2 / 5.202(a)(2) / 5101.603-3-90 (deep agency numbering)
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

_ROMAN_A = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
def alt_arabic(roman):
    """Normalize an alternate id to an arabic string: 'I'->'1', 'IV'->'4'; '' for empty/non-roman.
    Already-arabic input passes through ('1'->'1'). Keeps parser (arabic) and LLM (roman) edges on
    one key so they corroborate."""
    s = (roman or "").strip()
    if s.isdigit():
        return s
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = _ROMAN_A.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return str(total) if total else ""

def section_root(c):
    m = re.match(r"(\d+\.\d+(?:-\d+)*)", c or "")    # leading citation number (deep agency numbering ok)
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

AGENCY_PREFIX = re.compile(r"^[A-Za-z]+[-_](?=\d)")  # 'FAR-5.101'/'AFARS_5101.290' -> bare (leaves '6.302-2', 'subpart 25.4')

def strip_agency(c):
    return AGENCY_PREFIX.sub("", c or "")

_PAREN_TAIL = re.compile(r"(?:\([A-Za-z0-9]+\))+$")

def _citation_ok(c):
    """grammar_ok, tolerating a trailing paragraph qualifier on a subpart/part (e.g.
    'subpart 942.71(d)' — readable + root-valid, though the SUBPART anchor rejects the tail)."""
    return grammar_ok(c) or grammar_ok(_PAREN_TAIL.sub("", c).strip())

def normalize_target(t):
    """Clean a parser cross_reference target that is a raw ditaot href fragment
    ('Subpart_1901_4_T48_6052522' -> 'subpart 1901.4', 'AFARS_5101.290(b)' -> '5101.290(b)')
    into a same-regulation citation. Returns the cleaned citation when its root becomes grammar-
    valid, else the ORIGINAL target unchanged (opaque ids / cross-regulation refs stay raw, for
    R4's cross-reg edge handling / manual review). Idempotent (a clean target normalizes to itself)."""
    n = norm_cit(strip_agency(t))
    if _citation_ok(n):
        return n
    cand = norm_cit(strip_agency(X.href_to_citation("#" + (t or ""))))
    return cand if _citation_ok(cand) else (t or "")

# ---------- cross-regulation / companion classification (target_agency) ----------
# A cross_reference can point at ANOTHER regulation (DFARS -> its PGI, a separate store) or at a
# companion doc in the SAME store (PGI / Mandatory Procedure). Same-agency refs are the default and
# carry no extra fields; only the exceptions are marked: target_agency (a different STORE) and/or
# target_kind ('pgi' | 'mp', a companion doc class). This is the file-store shape of the SQL
# cross-regulation edge (from_node -> to_node with ref_type/confidence).
_PGI_RX = re.compile(r"(?:^|[ _\-/])PGI(?:[ _\-/]|$)", re.I)      # 'PGI 1.601' / 'DFARS_PGI_PGI_201.106'
_MP_RX = re.compile(r"(?:^|[ _\-])MP[ _]?\d", re.I)              # 'MP5305.303' / 'AFFARS_MP5301_9001' (DAFFARS)

def _tail_citation(t):
    """Bare citation from a foreign/companion target by dropping leading non-numeric tokens
    (split on '_'/space only, so a dashed subsection like '5349.501-70' stays intact)."""
    tail = _PAREN_TAIL.search(t)
    body = _PAREN_TAIL.sub("", t)
    body = body[:-5] if body.endswith(".dita") else body
    body = body.split("/")[0]
    toks = re.split(r"[ _]", body)
    while toks and not toks[0][:1].isdigit():
        toks.pop(0)
    cit = ""
    if len(toks) == 1 and re.match(r"^\d+\.\d", toks[0]):
        cit = toks[0]
    elif len(toks) >= 2 and toks[0].isdigit():
        cit = f"{toks[0]}.{toks[1]}" + ("-" + "-".join(toks[2:]) if len(toks) > 2 else "")
    return (cit + (tail.group(0) if tail else "")) if cit else t

def target_agency_of(target, current_agency):
    """Classify a cross_reference target -> (clean_target, target_agency, target_kind).
    target_agency is a DIFFERENT store when the ref crosses regulations (only DFARS<->DFARSPGI
    today), else the current agency. target_kind is 'pgi'/'mp' for a companion-doc reference, else
    ''. Same-agency regulation refs return (target, current_agency, '')."""
    t = (target or "").strip()
    if _PGI_RX.search(t):
        ag = "DFARSPGI" if current_agency in ("DFARS", "DFARSPGI") else current_agency
        return _tail_citation(t), ag, "pgi"
    if _MP_RX.search(t):
        return t, current_agency, "mp"                 # Mandatory Procedure companion (same store)
    return t, current_agency, ""

def _in_force(intervals, date):
    """Does any [from, to) interval contain `date`? With no date, treat as a union (any interval)."""
    if not intervals:
        return False
    if not date:
        return True
    return any((f is None or f <= date) and (t is None or date < t) for f, t in intervals)

def _resolves_asof(index, agency, bare, date):
    """Is `bare` (or its section root) a citation in `agency`'s regulation in force on `date`?"""
    m = (index or {}).get(agency)
    if not m:
        return False
    return _in_force(m.get(bare), date) or _in_force(m.get(section_root(bare)), date)

def resolve_reference(target, current_agency, index=None, as_of=None):
    """Fully + TEMPORALLY classify ONE cross_reference target -> (clean_target, target_agency,
    target_kind, validation), resolved against the corpus AS IT STOOD on `as_of` (the referencing
    row's edition date). This handles renumbering correctly: a stale old-number ref in a new edition
    resolves 'not_loaded', and the owning agency is judged by what was in force then.

      clean_target   bare citation; any agency/companion prefix stripped.
      target_agency  the agency whose regulation owned the target on `as_of` — == current_agency for
                     a same-agency ref, 'FAR' or another supplement for cross-regulation, 'DFARSPGI'
                     for a DFARS PGI ref.
      target_kind    'pgi' | 'mp' | '' — companion doc class within the target agency's store.
      validation     'resolves' | 'not_loaded' (grammar-valid but in no store as-of the date) |
                     'invalid' | '' (companion; not address-checked here).

    `index` is build_temporal_index()'s {agency -> {addr -> [(from, to)]}}; `as_of` the row's
    effective_from. Precedence: a companion prefix (PGI/MP) decides first (numbering collides with
    the parent, only the prefix disambiguates); otherwise the bare citation is resolved as-of the
    date against own agency, then FAR (the common parent), then the unique other agency that owned it
    then. No index -> assume same agency (temporality unavailable)."""
    t = (target or "").strip()
    if _PGI_RX.search(t):
        ag = "DFARSPGI" if current_agency in ("DFARS", "DFARSPGI") else current_agency
        cit = _tail_citation(t)
        v = "resolves" if _resolves_asof(index, ag, norm_cit(strip_agency(cit)), as_of) else ""
        return cit, ag, "pgi", v
    if _MP_RX.search(t):
        return t, current_agency, "mp", ""              # Mandatory Procedure companion (same store)
    bare = norm_cit(strip_agency(t))
    if not grammar_ok(bare):
        return t, current_agency, "", "invalid"
    if not index:
        return bare, current_agency, "", "resolves"     # no temporal index -> assume same agency
    if _resolves_asof(index, current_agency, bare, as_of):
        return bare, current_agency, "", "resolves"     # own content (incl self-reference)
    if current_agency != "FAR" and _resolves_asof(index, "FAR", bare, as_of):
        return bare, "FAR", "", "resolves"              # supplement -> FAR (the common cross-regulation)
    others = [a for a in index if a != current_agency and _resolves_asof(index, a, bare, as_of)]
    if others:                                          # any agency -> any other agency (as of the date)
        return bare, sorted(others)[0], "", "resolves"
    return bare, current_agency, "", "not_loaded"       # grammar-valid but in no store then: stale / uncaptured / spurious

def _dedup_append(cr, seen, kept):
    """Dedup a cross_ref by (target, alternate, target_agency, target_kind), merging mentions and
    keeping the stronger confidence. Shared by clean_targets / normalize_rows."""
    key = (norm_cit(cr["target"]), cr.get("alternate", ""),
           cr.get("target_agency", ""), cr.get("target_kind", ""))
    if key in seen:
        seen[key].setdefault("mentions", []).extend(cr.get("mentions", []))
        if cr.get("confidence") == "explicit":
            seen[key]["confidence"] = "explicit"
        return
    seen[key] = cr
    kept.append(cr)

def clean_targets(rows):
    """Light pre-reconcile pass: rewrite raw ditaot href targets to clean citations + dedup. NO
    agency/validation classification (that is normalize_rows, run standalone with the temporal
    index). Used inside the LLM pass so parser refs corroborate the LLM's clean citations. Returns
    #targets changed."""
    changed = 0
    for r in rows:
        if r.get("alternate") or r["type"] not in ("section", "subsection"):
            continue
        seen, kept = {}, []
        for cr in r.get("cross_references", []):
            nt = normalize_target(cr["target"])
            if nt != cr["target"]:
                cr["target"] = nt
                changed += 1
            _dedup_append(cr, seen, kept)
        r["cross_references"] = kept
    return changed

def normalize_rows(rows, agency, index=None):
    """Full deterministic + TEMPORAL classification (the brains of `references --normalize-only`):
    for every base row's cross_references, clean the target and resolve target_agency / target_kind /
    validation AS OF that row's edition date (effective_from), via the global temporal address index.
    Only exceptions carry extra fields — a same-agency regulation ref has no target_agency/target_kind.
    Idempotent (re-running corrects a prior mis-tag). Mutates rows; returns #targets changed."""
    changed = 0
    for r in rows:
        if r.get("alternate") or r["type"] not in ("section", "subsection"):
            continue
        as_of = r.get("effective_from")
        seen, kept = {}, []
        for cr in r.get("cross_references", []):
            nt = normalize_target(cr["target"])
            cit, tag_ag, tag_kind, valid = resolve_reference(nt, agency, index, as_of)
            if tag_ag != agency:
                cr["target_agency"] = tag_ag
            elif "target_agency" in cr:
                del cr["target_agency"]                  # correct a prior mis-tag (idempotent)
            if tag_kind:
                cr["target_kind"] = tag_kind
            elif "target_kind" in cr:
                del cr["target_kind"]
            if valid:
                cr["validation"] = valid
            if cit != cr["target"]:
                cr["target"] = cit
                changed += 1
            _dedup_append(cr, seen, kept)
        r["cross_references"] = kept
    return changed

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
                "locator": it.get("locator", ""), "alternate": it.get("alternate", ""),
                "status": it["status"], "by": "auto"}
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
    # The LLM output for a unit can be malformed (truncated / oddly-shaped JSON — e.g. a bare string or
    # a list with a stray element). Coerce each unit's refs to a list of dicts so one bad result can't
    # crash reconcile; that unit just contributes fewer LLM refs (its parser refs are unaffected).
    llm_by_cit = {c: [r for r in (v if isinstance(v, list) else []) if isinstance(r, dict)]
                  for c, v in (llm_by_cit or {}).items()}
    # Only BASE units go through reconcile. Flat alternate rows share their base's citation and would
    # collide here; their own (parser-found) refs are tagged parser_only by `apply`.
    units = [r for r in rows if r["type"] in ("section", "subsection") and not r.get("alternate")]
    for u in units:
        cit = u["citation"]
        self_cit = norm_cit(strip_agency(cit))             # exact self-reference (e.g. 5.101 -> 5.101 / "this section")

        # ----- internal references (FAR -> FAR) -----
        # Key by (base citation, alternate): a reference to a clause Alternate is a DISTINCT edge from
        # a reference to the base clause, so "52.204-30" and its Alternate 1 reconcile separately.
        # Alternate ids are normalized to arabic so the parser (arabic field) and LLM (roman in target)
        # land on the same key.
        parser_map = {}                                    # (base norm target, alternate) -> {kind, evidence}
        for cr in u["cross_references"]:
            if cr.get("target_agency") or cr.get("target_kind"):
                continue                                   # cross-store / companion ref: not same-reg-internal, kept parser-only by apply
            t = norm_cit(cr["target"])                     # parser carries the variant in `alternate`, not the target
            alt = alt_arabic(cr.get("alternate", ""))
            if t and t != self_cit:
                parser_map.setdefault((t, alt), {"kind": cr.get("confidence", "inferred"),
                                                 "evidence": cr_evidence(cr)})
        llm_map = {}                                       # (base norm target, alternate) -> {evidence, validation}
        for ref in llm_by_cit.get(cit, []):
            if ref.get("scope") == "external":
                continue
            raw = ref.get("target", "")
            if not raw:
                continue
            base, roman = split_alternate(raw)             # LLM may emit 'X Alternate I' in the target …
            alt = alt_arabic(ref.get("alternate", "") or roman)   # … or a separate `alternate` field
            t, status = validate(base, addr_map)
            if t and t != self_cit:
                llm_map.setdefault((t, alt), {"evidence": ref.get("evidence", ""), "validation": status})
        for key in sorted(set(parser_map) | set(llm_map), key=lambda k: (cit_sort_key(k[0]), k[1])):
            t, alt = key
            p, l = parser_map.get(key), llm_map.get(key)
            if p and l:
                status = "corroborated"
            elif p:
                status = "parser_explicit" if p["kind"] == "explicit" else "parser_inferred"
            else:
                status = "llm_only"
            stats[status] += 1
            ledger.append({
                "unit": cit, "url": u["url"], "scope": "internal", "target": t, "status": status,
                "alternate": alt,                          # FAR clause variant (arabic '1'/'2'/…); '' for the base clause
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
