#!/usr/bin/env python3
"""Prompts + JSON schemas for the reference-verification LLM stages — the TASK LOGIC the
transport (chunker.llm.client) is deliberately kept free of.

These are ported verbatim from pipeline/gemini_audit.py. They are parameterized by
{regulation} + {citation}, so pointing the pass at a supplement is just passing
regulation="DFARS" etc. The citation-format *examples* remain FAR-shaped; supplements mirror
FAR numbering (Part 52 <-> DFARS 252, etc.), and the per-agency grammar generalization (R4)
happens in reconcile/extract, not here.

PROMPT_VERSION folds into the cache key: bump it whenever a prompt below changes so stale
cached model responses are invalidated.
"""

PROMPT_VERSION = "v10"

# ---------- blind audit (per unit: list every cross-reference, one atomic target each) ----------
AUDIT_SYSTEM = (
    "You audit cross-references in a government regulation. You are given the COMPLETE raw text "
    "of ONE unit of {regulation} (its citation is {citation}). Find EVERY reference it makes "
    "to another part / subpart / section / subsection / paragraph of {regulation} (this same "
    "regulation), in any of these forms:\n"
    "1. Explicit link: <xref href=\"...\">...</xref> -- the href names the target.\n"
    "2. Link + a parenthetical, e.g. <xref ...>5.202</xref>(a)(2). USUALLY the parenthetical "
    "narrows the link (-> 5.202(a)(2)) -- but DO NOT assume it; read the sentence. Sometimes it "
    "attaches to THIS section, e.g. 'the authority of 5.202 and (a)(2) of this section' means BOTH "
    "5.202 and {citation}(a)(2). Resolve each to what the text actually means.\n"
    "3. PROSE REFERENCES WITH NO <xref> LINK -- a citation written in plain text, not wrapped in a "
    "tag: 'as required by 5.207', 'see 6.302', 'under subpart 9.4', 'paragraph (b) of this section' "
    "(resolve 'this section/paragraph' against {citation}). PAY SPECIAL ATTENTION to these: automated "
    "XML-tag scanning already catches every <xref>, so references with NO tag are exactly what it "
    "misses -- they are the most valuable for you to surface. Scan the prose carefully for them.\n"
    "4. Ranges, in any phrasing -- '(a) through (f)', '1 to 3', '(a)-(d)', '52.219-3 through "
    "52.219-5'. EXPAND every range into its individual members and return ONE reference per member "
    "(do NOT emit a span like '5.203(a)-(d)'). E.g. '5.203(a) through (d)' -> four references: "
    "5.203(a), 5.203(b), 5.203(c), 5.203(d). Give each member the SAME `evidence` (the range "
    "sentence). Only expand when the members are unambiguous; if you cannot tell the sequence, "
    "report the endpoints you are sure of.\n"
    "5. CITATION + PARAGRAPH LIST -- a citation followed by a comma list of bare paragraphs carries "
    "the citation's number across the WHOLE list, e.g. 'the exemptions at 5.202(a)(1), (a)(4) through "
    "(a)(9), or (a)(11)' means 5.202(a)(1), 5.202(a)(4)..5.202(a)(9), and 5.202(a)(11) -- ALL under "
    "5.202, NOT this section. Resolve each bare '(x)' against the most recent explicit citation number, "
    "not against {citation}.\n"
    "6. CLAUSE ALTERNATES -- a FAR clause can have variant versions called Alternates ('Alternate I', "
    "'Alternate II', …). Report a reference to one as a reference to the BASE clause, with `alternate` "
    "set to the roman numeral as written. TWO syntaxes are equally common -- catch BOTH:\n"
    "   (a) 'Alternate I of 52.204-30'  ->  target '52.204-30', alternate 'I'.\n"
    "   (b) '52.203-6, Restrictions … (Jun 2020), with Alternate I' -- here 'with Alternate <N>' "
    "(also 'with its Alternate <N>', 'X and Alternate <N>') modifies the clause cited JUST BEFORE it, "
    "so -> target '52.203-6', alternate 'I'. It is NOT a separate clause and NOT a reference to the "
    "section being read.\n"
    "A clause's Alternate is a DISTINCT reference from the base clause: when the text lists both "
    "(e.g. '(i) 52.219-6 … (ii) Alternate I of 52.219-6'), report TWO references -- one with "
    "`alternate` omitted and one with `alternate`='I'. Leave `alternate` unset for ordinary references.\n"
    "EXCLUDE SELF-REFERENCES: do NOT report a reference from this unit to itself -- 'this section', or "
    "the bare citation {citation}, is the document referring to itself and is not a cross-reference "
    "(but a DIFFERENT paragraph of this section, e.g. {citation}(a)(2), IS a valid reference).\n"
    "For each reference set `scope`:\n"
    " - scope='internal' for references to ANOTHER part of {regulation} (the cases above). `target` is "
    "the {regulation} citation in standard form (e.g. 5.202, 5.202(a)(2), 6.302-2, subpart 9.4).\n"
    " - scope='external' ONLY for a reference written in one of these five strict citation formats: "
    "U.S.C. (e.g. '41 U.S.C. 1303(a)'), CFR ('13 CFR 128.300'), Executive Order ('E.O. 11246'), Public "
    "Law ('Pub. L. 118-31'), or OMB Circular ('OMB Circular A-76'). Set `target` to the citation as "
    "written and `ref_type` to one of usc|cfr|eo|public_law|omb. Expand an external range only if "
    "unambiguous. Do NOT report anything else as external -- NO named statutes/Acts (e.g. 'the Small "
    "Business Act'), program names, agency names, form numbers, treaties, standards, or web addresses. "
    "If it is not in one of those five formats, omit it.\n"
    "Exclude only bare web URLs/emails and DITA plumbing. As `evidence`, give the COMPLETE sentence(s) "
    "containing the reference, quoted VERBATIM, with the exact citation text wrapped in « » guillemets "
    "-- e.g. 'The contracting officer shall, as required by «5.207», publicize the action.' Quote "
    "enough surrounding text to judge the reference; do not paraphrase or shorten."
)
AUDIT_SCHEMA = {
    "type": "array",
    "items": {"type": "object",
              "properties": {"target": {"type": "string"}, "evidence": {"type": "string"},
                             "scope": {"type": "string", "enum": ["internal", "external"]},
                             "ref_type": {"type": "string"},
                             "alternate": {"type": "string"}},   # clause Alternate variant (roman); '' for base
              "required": ["target", "evidence"]},
}

# ---------- judge (reconcile disagreements: accept / manual / reject) ----------
JUDGE_SYSTEM = (
    "You reconcile cross-reference disagreements for {regulation} unit {citation}. You are given the "
    "raw text and a numbered list of DISAGREEMENTS — each is a SINGLE atomic citation that EITHER "
    "the deterministic parser found (from prose/an expanded range, not a tagged link) OR an "
    "independent LLM found, but not both. For EACH, read the source and decide whether it is a real, "
    "correct cross-reference FROM this unit TO that target within {regulation}: choose 'accept' (the "
    "citation is correct as written), 'manual' (a real reference but the citation is wrong — put the "
    "correct citation(s) in `value`), or 'reject' (not a real reference to this regulation — external, "
    "mis-parsed, or hallucinated). Give a one-sentence `rationale`. Return one object per disagreement "
    "with its `n`. A target may be a clause Alternate, shown as e.g. '52.204-30 Alternate I' -- a "
    "legitimate variant reference, DISTINCT from the base clause; accept it if the unit really "
    "incorporates that alternate."
)
JUDGE_SCHEMA = {
    "type": "array",
    "items": {"type": "object",
              "properties": {"n": {"type": "integer"},
                             "choice": {"type": "string", "enum": ["accept", "manual", "reject"]},
                             "value": {"type": "array", "items": {"type": "string"}},
                             "rationale": {"type": "string"}},
              "required": ["n", "choice", "rationale"]},
}


def judge_user_text(unit_cit, discrepancies):
    """Render the disagreement list for the judge. discrepancies: [{n, target, source, evidence,
    alternate?}]. The unit's raw text is prepended by the caller."""
    lines = [f"Unit {unit_cit}. Disagreements to resolve:"]
    for d in discrepancies:
        alt = f" Alternate {d['alternate']}" if d.get("alternate") else ""   # show the variant being judged
        lines.append(f"  [{d['n']}] (found by {d['source']}) target={d['target']}{alt} | "
                     f"evidence: {d.get('evidence', '')[:300]}")
    return "\n".join(lines)


# ---------- doc_class fold-in (R6): per-unit regulation-vs-companion verdict ----------
# NOTE: drafted in R6. The two-way classify validation (promote an appendix that reads as
# regulation; demote a mis-captured reg) replaces the retired standalone classifier pass.
# Kept here as the home for that prompt when R6 lands.
