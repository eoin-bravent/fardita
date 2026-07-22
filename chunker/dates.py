"""Date-coverage report (ported from pipeline/canon.py).

Every edition should carry an effective_date (the legal temporal key); every GIT-sourced
(gsa-github) edition/row should carry a commit_date. Archive editions carry a folder token
as source_commit (NOT a git sha), so they are excluded from the commit_date expectation.
Pure store logic — no lxml, no network.
"""
GIT_SOURCES = {"gsa-github"}


def _is_git(obj):
    return obj.get("source") in GIT_SOURCES and bool(obj.get("source_commit"))


def date_coverage(store):
    """Honest, store-level date-tracking report (safe to persist; no chunk text)."""
    eds = store.editions
    ed_git = [e for e in eds if _is_git(e)]
    rows_git = [r for r in store.rows if _is_git(r)]
    c = {
        "editions": len(eds),
        "editions_with_effective": sum(1 for e in eds if e.get("effective_date")),
        "editions_git": len(ed_git),
        "editions_git_with_commit": sum(1 for e in ed_git if e.get("commit_date")),
        "rows": len(store.rows),
        "rows_git": len(rows_git),
        "rows_git_with_commit": sum(1 for r in rows_git if r.get("commit_date")),
    }
    eff_dates = sorted(e["effective_date"] for e in eds if e.get("effective_date"))
    c["floor"] = eff_dates[0] if eff_dates else None        # earliest edition (corpus "from")
    c["ceiling"] = eff_dates[-1] if eff_dates else None      # latest edition (corpus "to")
    c["current"] = any(r.get("effective_to") is None for r in store.rows)  # latest still in force
    c["effective_ok"] = c["editions_with_effective"] == c["editions"]
    c["commit_ok"] = (c["editions_git_with_commit"] == c["editions_git"]
                      and c["rows_git_with_commit"] == c["rows_git"])
    c["ok"] = c["effective_ok"] and c["commit_ok"]
    return c
