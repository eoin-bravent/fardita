#!/usr/bin/env python3
"""Change track (stage parallel to chunking): parse LSATable.dita — the List of Sections Affected —
into structured changelog entries.

This is DETERMINISTIC on purpose: the LSA table is clean, structured DITA (a 3-column table —
affected section, plain-language description, Federal Register case link), so there is exactly one
correct reading. No blind-LLM / reconcile / judge machinery (that exists for the *ambiguous* job of
finding cross-references; there's nothing fuzzy to cross-check here).

Output: a flat list of change entries, each stamped with the FAC (source_version) + pipeline_version,
mirroring the chunk format so the change track can load into the same versioned store. The entries
feed the FAC-change tools (Amendatory Instruction Generator, Regulation Change Summarizer).
"""
import re
import xml.etree.ElementTree as ET

LSA_DEFAULT = "LSATable.dita"

def _text(el):
    """Whitespace-collapsed text of an element (all descendants)."""
    return " ".join("".join(el.itertext()).split())

def extract_rev_changes(path):
    """PI-preserving parse → document-ordered marker metadata for each rev-marked element:
    [{fac, case_number, why}, ...], one per `rev` span in document order.

    ElementTree's default parse DROPS the <?FM MARKER [CaseNumber]/[Why]?> processing
    instructions, so we re-parse with insert_pis=True (the XML parser enforces the PI's `?>`
    boundary per spec, so each marker's text is captured complete). The changed *text* itself is
    NOT taken here — insert_pis would fold marker text into the flattened content — so the chunker
    reads the span text from its own (PI-free) parse and aligns to this list by document order.
    Returns [] if there are no rev spans or the file can't be parsed."""
    try:
        raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
        if "rev=" not in raw:                              # fast skip — almost no files carry rev
            return []
        root = ET.fromstring(raw, parser=ET.XMLParser(target=ET.TreeBuilder(insert_pis=True)))
    except (ET.ParseError, OSError):
        return []
    items = []
    for el in root.iter():
        if not el.get("rev"):
            continue
        case_number, why = "", ""
        for pi in el.iter():
            if pi.tag is ET.PI and pi.text:               # the marker PIs live inside the rev span
                if "[CaseNumber]" in pi.text:
                    case_number = pi.text.split("[CaseNumber]", 1)[1].strip()
                elif "[Why]" in pi.text:
                    why = " ".join(pi.text.split("[Why]", 1)[1].split())
        items.append({"fac": el.get("rev") or "", "case_number": case_number, "why": why})
    return items

def parse_lsa(path, regulation="FAR", source_version="", pipeline_version=""):
    """Parse an LSA table file -> list of change entries. Returns [] if the file is absent,
    unparseable, or has no LSA table. Each entry:
        { regulation, source_version, pipeline_version,
          section, citation, paragraphs[], description, case_number, federal_register_url }
    """
    try:
        raw = re.sub(r"<!DOCTYPE.*?>", "", open(path, encoding="utf-8").read(), flags=re.S)
        root = ET.fromstring(raw)
    except (ET.ParseError, OSError):
        return []
    table = (next((t for t in root.iter("table") if t.get("otherprops") == "LSA"), None)
             or root.find(".//table"))
    if table is None:
        return []
    # The table's own title carries the FAC stamp ("FAC 2026-01 March 13, 2026"); prefer it,
    # fall back to the run's source_version (the ditamap rev — same value).
    ttl = table.find("./title")
    fac = (_text(ttl) if ttl is not None else "") or source_version
    tbody = table.find(".//tbody")
    if tbody is None:
        return []
    entries = []
    for row in tbody.findall("./row"):
        cells = row.findall("./entry")
        if len(cells) < 3:
            continue
        sec_cell, desc_cell, case_cell = cells[0], cells[1], cells[2]
        # Section column: the fm:ParaNumOnly xref is the section number; fm:List xrefs are paragraph locators.
        section, paragraphs = "", []
        for x in sec_cell.findall(".//xref"):
            t = _text(x)
            if not t:
                continue
            if (x.get("outputclass") == "fm:ParaNumOnly" or (not section and re.match(r"^\d+\.\d+", t))):
                if not section:
                    section = t.replace(" ", "")
            elif re.match(r"^\(", t):                       # a paragraph locator like "(b)(2)"
                paragraphs.append(t.replace(" ", ""))
        description = " ".join(_text(p) for p in desc_cell.findall(".//p")) or _text(desc_cell)
        case_x = case_cell.find(".//xref")
        case_number = _text(case_x) if case_x is not None else _text(case_cell)
        fr_url = (case_x.get("href") if case_x is not None else "") or ""
        entries.append({
            "regulation": regulation,
            "source_version": fac,
            "pipeline_version": pipeline_version,
            "section": section,
            "citation": f"{regulation}-{section}" if section else "",
            "paragraphs": paragraphs,
            "description": description,
            "case_number": case_number,
            "federal_register_url": fr_url,
        })
    return entries
