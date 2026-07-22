"""Filesystem layout for chunker — the one place that knows where things live.

Standardizes the store path to stores/<AG>/store.json (Decision E: no <AG>_ prefix, no FAR
pipeline/store special-case). Source data (archives, git clones) stays at the repo root,
reused in place (Decision: reprocess the on-disk archive, no host-side re-download)."""
import os
import json
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root
PKG = os.path.join(ROOT, "chunker")
DATA_DIR = os.path.join(PKG, "data")
CACHE_DIR = os.path.join(PKG, "cache")           # era surveys + hints (regenerable)
STORES_DIR = os.path.join(ROOT, "stores")
CANON_DIR = os.path.join(ROOT, "canon_git")      # full-history GSA GitHub clones

ARCHIVE_ROOT = os.path.join(ROOT, "archive")             # per-agency supplement editions
ARCHIVE_FAR = os.path.join(ROOT, "archive_far")          # FAR editions (flat layout)
ARCHIVE_AFARS = os.path.join(ROOT, "archive_afars", "AFARS")

_AGENCIES = None


def agencies_cfg():
    global _AGENCIES
    if _AGENCIES is None:
        with open(os.path.join(DATA_DIR, "agencies.json"), encoding="utf-8") as f:
            _AGENCIES = json.load(f)
    return _AGENCIES


def agencies():
    """FAR first, then A-Z (matches the pipeline's display/run order)."""
    return sorted((a for a in agencies_cfg() if not a.startswith("_")),
                  key=lambda a: (a != "FAR", a))


def store_dir(ag, base=None):
    return os.path.join(base or STORES_DIR, ag)          # stores/<AG>/  (uniform)


def archive_dir(ag):
    if ag == "FAR":
        return ARCHIVE_FAR
    if ag == "AFARS":
        return ARCHIVE_AFARS
    sub = (agencies_cfg().get(ag) or {}).get("archive_agency", ag)   # DEAR -> DEARS
    return os.path.join(ARCHIVE_ROOT, sub)


def canon_repo(ag):
    return os.path.join(CANON_DIR, ag)


def pipeline_rev():
    """Short git HEAD of the repo — stamped as pipeline_version (provenance only; NOT in
    content_hash, so it never affects versioning/dedup)."""
    try:
        p = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else "chunker"
    except Exception:
        return "chunker"
