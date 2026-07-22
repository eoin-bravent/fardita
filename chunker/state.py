"""One per-agency state file — stores/<AG>/state.json.

Collapses the old pipeline_state.json + verification.json + date_coverage.json into ONE
file with sections {agency, steps, verification, companion, dates, at} (REFACTOR_PLAN §2).
It coexists in the same file with Store's ingest-cursor keys (e.g. 'gsa-github', written by
Store.save_state) — every write is load / merge / atomic-replace, so unknown keys survive.
The dashboard reads ONLY this file (never the big store).
"""
import os
import json
import time


def _load(path):
    if os.path.exists(path):
        try:
            v = json.load(open(path, encoding="utf-8"))
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class State:
    """Read/update the per-agency state.json, preserving keys this class doesn't own."""

    def __init__(self, store_dir, agency):
        self.dir = os.path.abspath(store_dir)
        self.agency = agency
        self.path = os.path.join(self.dir, "state.json")

    def _update(self, fn):
        os.makedirs(self.dir, exist_ok=True)
        d = _load(self.path)
        fn(d)
        d["agency"] = self.agency
        d["at"] = _now()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def mark_step(self, step, status, **info):
        """Record a pipeline step's status (download/survey/backfill/canon/audit/…)."""
        self._update(lambda d: d.setdefault("steps", {}).__setitem__(
            step, {"status": status, "at": _now(), **info}))

    def set_section(self, name, value):
        """Set a whole section (verification | companion | dates)."""
        self._update(lambda d: d.__setitem__(name, value))

    def get(self):
        return _load(self.path)
