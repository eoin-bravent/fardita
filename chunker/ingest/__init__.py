"""Unified ingest — the ONE merge step every source calls.

Both the archive backfill and the GitHub canon paths reduce to: chunk an edition into rows,
then `ingest_edition(...)` = optional cosmetic collapse (the archive<->GitHub seam) followed
by the SCD-2 `merge_snapshot`. The PARTIAL-edition rule (a snapshot covering < threshold of
the store's current view merges add/update-only) is centralized here, identical to the logic
in the old backfill_archives.py and canon.py._ingest.
"""
from chunker.parsers import collapse_cosmetic


def _units(rows):
    """Top-level section citations (no paragraph refs, no alternates) — the coverage basis
    for the complete-vs-PARTIAL decision."""
    return {r["citation"] for r in rows
            if "(" not in r["citation"] and not r.get("alternate")}


def ingest_edition(store, rows, effective_date, source_version, *, source, source_commit,
                   commit_date="", complete=None, collapse=True, drop_quotes=False,
                   partial_threshold=0.5):
    """Merge one edition snapshot into `store`. Returns (stats, collapsed_keys).

    complete=None auto-decides PARTIAL from coverage (matches the archive/canon drivers);
    pass True/False to force. collapse runs the cosmetic seam first (no-op on an empty store).
    """
    collapsed = []
    if collapse:
        rows, collapsed = collapse_cosmetic(rows, store, effective_date, drop_quotes=drop_quotes)
    if complete is None:
        cur = _units(store.current_rows())
        snap = _units(rows)
        complete = (not cur) or (len(snap) >= partial_threshold * len(cur))
    stats = store.merge_snapshot(rows, effective_date, source_version, source=source,
                                 source_commit=source_commit, commit_date=commit_date,
                                 complete=complete)
    return stats, collapsed
