"""Route captured companion units into the per-agency companion store (Decision A).

`chunk_edition_canon` (with cfg['capture_companions']) stashes companion units in
manifest['companions'] as {doc_class, file, rows}. This module re-keys each unit to its
class-prefixed citation `<AG>-<CLASS>-<localid>`, tags `doc_class` / `companion_of` /
`prescribed_by`, sets `regulation = '<AG>-COMPANION'`, and merges them into companion.json via
the same SCD-2 `ingest_edition` — so companions get full temporal history + collapse-dedup,
identical to the regulation store, kept in a sibling file for clean filtering + honest
reg-only completeness (COMPANION_DOCS §3, §8).
"""
import re
from chunker.parsers import _adapter as A
from chunker.ingest import ingest_edition

CANON_SOURCE = "gsa-github"


def _prescribed_by(agency, localid):
    """MP5301.601 <-> reg 5301.601 (COMPANION_DOCS §9): when the class-stripped localid is a
    numeric section, it maps to the regulation citation it implements; '' otherwise (annexes
    /attachments named 'ANNEX 1' don't map — the reference LLM fills those later)."""
    return f"{agency}-{localid}" if re.match(r"^\d+\.\d", localid or "") else ""


def build_rows(companions, agency):
    """manifest['companions'] -> tagged companion chunk rows with re-keyed identity."""
    reg = f"{agency}-COMPANION"
    out = []
    for c in companions:
        rows = c.get("rows") or []
        if not rows:
            continue
        dc = c["doc_class"]
        base_old = (rows[0].get("citation", "") or "").split("(")[0]
        cit_base, localid = A.companion_identity(
            agency, dc, base_old, rows[0].get("section_title", ""), c.get("file", ""))
        presc = _prescribed_by(agency, localid)
        for r in rows:
            old = r.get("citation", "") or ""
            suffix = old[len(old.split("(")[0]):]        # keep any '(a)(1)' paragraph tail
            row = dict(r)
            row["regulation"] = reg
            row["doc_class"] = dc
            row["companion_of"] = agency
            if presc and not row.get("prescribed_by"):
                row["prescribed_by"] = presc
            row["citation"] = cit_base + suffix
            out.append(row)
    return out


def route(companion_store, companions, agency, effective_date, *, source_commit,
          commit_date="", source=CANON_SOURCE, collapse=True):
    """Merge one edition's companion snapshot into companion_store. Returns (stats, collapsed)."""
    rows = build_rows(companions, agency)
    if not rows:
        return {k: 0 for k in ("snapshot_rows", "new", "changed", "unchanged")}, []
    return ingest_edition(companion_store, rows, effective_date,
                          f"companion {(source_commit or '')[:9]}", source=source,
                          source_commit=source_commit, commit_date=commit_date,
                          complete=True, collapse=collapse)
