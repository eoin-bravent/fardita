"""Canonical GitHub DITA source (GSA repos) -> the versioned store.

Ported from pipeline/canon.py: clone/pull the full-history repo, chunk the DITA with
parsers.chunk_edition_canon, merge via the unified ingest_edition. The ONE behavioral
change (the resolved-rule fix): `replay` consumes the agency's EDITION SOURCE (grouped,
marker/FAC-dated editions from editions.py) and ingests each settled commit at its edition
effective_date — instead of the old raw per-commit replay that manufactured one edition per
commit dated by commit date. Junk (Steven/Sandbox/…) and unknown-dated editions are skipped
and reported (never guessed).
"""
import os
import json
import shutil
import tarfile
import tempfile
import subprocess

from chunker import paths
from chunker import parsers
from chunker.ingest import ingest_edition
from chunker.ingest import editions
from chunker.ingest.editions import _git

CANON_SOURCE = "gsa-github"
REPO_URL = "https://github.com/GSA/GSA-Acquisition-{src}"
# agencies whose canon lives in ANOTHER agency's repo (+ which ditamap to read there)
ALIAS = {"DFARSPGI": ("DFARS", "PGI.ditamap")}
# no public GitHub repo -> canon comes from the acquisition.gov zip instead (host-side)
NO_REPO = {"TRANSFARS"}


def _effective_date(ag, commit_date):
    """acquisition.gov effective date for the agency's CURRENT edition (canon_dates.json,
    host-side scraped), else the git commit date. CURRENT HEAD only — historical replay
    editions keep their own marker/commit date (never guessed for non-FAR)."""
    try:
        d = json.load(open(os.path.join(paths.DATA_DIR, "canon_dates.json"),
                           encoding="utf-8")).get(ag.upper())
    except Exception:
        d = None
    if isinstance(d, dict):
        d = d.get("effective_date")
    return d or commit_date


def _src(ag):
    return ALIAS[ag][0] if ag in ALIAS else ag


def _ditamap(ag):
    return ALIAS[ag][1] if ag in ALIAS else None


def repo_dir(ag):
    return paths.canon_repo(_src(ag))


def head_info(repo):
    from chunker.ingest.editions import _git
    sha = (_git(repo, "rev-parse", "HEAD", ok_fail=True) or "").strip()
    date = (_git(repo, "log", "-1", "--format=%cs", ok_fail=True) or "").strip()
    return sha, date


# ---------------------------------------------------------------- download / pull
def download(ag, shallow=False):
    """Clone (full history, for replay) or pull the agency's canon repo. Idempotent:
    clears a stale non-repo path first so a re-run always yields a real clone."""
    from chunker.ingest.editions import _git
    if ag in NO_REPO:
        return None
    d = repo_dir(ag)
    if os.path.isdir(os.path.join(d, ".git")):
        _git(d, "fetch", "--all", "--quiet", ok_fail=True)
        _git(d, "pull", "--ff-only", "--quiet", ok_fail=True)
        return d
    if os.path.islink(d) or os.path.isfile(d):
        try:
            os.unlink(d)
        except OSError:
            pass
    elif os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    if os.path.lexists(d):
        raise RuntimeError(f"{d} exists and is not a git repo (stale symlink/dir?); "
                           f"remove it manually and re-run")
    os.makedirs(paths.CANON_DIR, exist_ok=True)
    url = REPO_URL.format(src=_src(ag))
    cmd = ["git", "clone", "--progress"] + (["--depth", "1"] if shallow else []) + [url, d]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if subprocess.run(cmd, env=env).returncode != 0:
        raise RuntimeError(f"clone failed for {ag} ({url})")
    return d


# ---------------------------------------------------------------- chunk one tree
def export_at(repo, sha, dest):
    """Extract the repo's dita/ tree at <sha> into dest (git archive -> tar)."""
    rel = "dita" if os.path.isdir(os.path.join(repo, "dita")) else "."
    p = subprocess.run(["git", "-C", repo, "archive", "--format=tar", sha, rel],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git archive {sha[:9]} failed")
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
        tf.write(p.stdout)
        tar_path = tf.name
    try:
        with tarfile.open(tar_path) as t:
            t.extractall(dest)
    finally:
        os.unlink(tar_path)
    return dest


def _cfg(ag):
    cfg = parsers.default_cfg(ag)
    cfg["pipeline_version"] = "canon"
    dm = _ditamap(ag)
    if dm:
        cfg["ditamap"] = dm
    return cfg


def chunk_head(ag, cfg=None):
    """Chunk the working-tree HEAD directly (no export)."""
    return parsers.chunk_edition_canon(repo_dir(ag), cfg or _cfg(ag), {})


def chunk_commit(ag, sha, cfg=None):
    """Chunk a specific historical commit via a temp export."""
    cfg = cfg or _cfg(ag)
    tmp = tempfile.mkdtemp(prefix=f"canon_{ag}_")
    try:
        export_at(repo_dir(ag), sha, tmp)
        return parsers.chunk_edition_canon(tmp, cfg, {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- ingest / replay
def _done_shas(store):
    return {e.get("source_commit") for e in store.editions}


def replay(store, ag, source=None, save_every=10, on_edition=None, companion_store=None):
    """Ingest the agency's GitHub editions per its edition source (grouped, marker/FAC-dated),
    oldest-first, into `store`. Skips junk + unknown-dated editions (recorded in the report).
    When `companion_store` is given, companion units are captured (Decision A) and merged into
    it at the same effective dates. Returns {agency, source, editions, skipped, companion_editions}."""
    if ag in NO_REPO:
        return {"agency": ag, "source": "none", "editions": [], "skipped": [],
                "note": "no GitHub repo (archive-only)"}
    repo = repo_dir(ag)
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise RuntimeError(f"{ag}: no clone at {repo} -- run download() first")
    source = source or editions.source_for(ag)
    plan = source.plan(repo, ag)
    cfg = _cfg(ag)
    if companion_store is not None:
        cfg["capture_companions"] = True
    done = _done_shas(store)
    report = {"agency": ag, "source": source.kind, "editions": [], "skipped": [],
              "companion_editions": []}
    dated = [e for e in plan if e["effective_date"] and not e["junk"]]
    n = len(dated)
    k = 0
    for ed in plan:
        if ed["junk"]:
            report["skipped"].append({"label": ed["label"][:40], "reason": "junk",
                                      "sha": ed["settled_sha"][:9]})
            continue
        if not ed["effective_date"]:
            report["skipped"].append({"label": ed["label"][:40], "reason": "unknown-date",
                                      "sha": ed["settled_sha"][:9]})
            continue
        if ed["settled_sha"] in done:
            continue
        k += 1
        try:
            rows, man = chunk_commit(ag, ed["settled_sha"], cfg)
        except Exception as e:
            report["editions"].append({"label": ed["label"][:40], "date": ed["effective_date"],
                                       "error": repr(e)[:200]})
            continue
        if not rows and not man.get("companions"):
            report["editions"].append({"label": ed["label"][:40], "date": ed["effective_date"],
                                       "chunks": 0})
            continue
        stats, collapsed = ingest_edition(
            store, rows, ed["effective_date"], f"gh {ed['settled_sha'][:9]}",
            source=CANON_SOURCE, source_commit=ed["settled_sha"],
            commit_date=ed["commit_date"], complete=None)
        rec = {"label": ed["label"][:40], "date": ed["effective_date"],
               "sha": ed["settled_sha"][:9], "chunks": len(rows), "new": stats["new"],
               "changed": stats["changed"], "extended_back": stats["extended_backward"],
               "collapsed": len(collapsed)}
        if companion_store is not None:
            from chunker.ingest import companion as _fcomp
            cstats, _cc = _fcomp.route(
                companion_store, man.get("companions", []), ag, ed["effective_date"],
                source_commit=ed["settled_sha"], commit_date=ed["commit_date"])
            rec["companion_new"] = cstats.get("new", 0)
            rec["companion_rows"] = cstats.get("snapshot_rows", 0)
            report["companion_editions"].append(
                {"date": ed["effective_date"], "new": cstats.get("new", 0),
                 "rows": cstats.get("snapshot_rows", 0)})
        report["editions"].append(rec)
        if k % save_every == 0:
            store.save()
            if companion_store is not None:
                companion_store.save()
        if on_edition:
            on_edition(k, n, ed, stats)
    store.save()
    if companion_store is not None:
        companion_store.save()
    return report


def ingest_head(store, ag, effective_date=None, companion_store=None):
    """Ingest just the current HEAD as one edition (ongoing-update / HeadSource path).
    When `companion_store` is given, companion units are captured into it at the same date."""
    if ag in NO_REPO:
        return None
    repo = repo_dir(ag)
    sha, cdate = head_info(repo)
    if sha in _done_shas(store):
        return {"agency": ag, "note": f"HEAD {sha[:9]} already ingested"}
    cfg = _cfg(ag)
    if companion_store is not None:
        cfg["capture_companions"] = True
    rows, man = chunk_head(ag, cfg)
    if not rows and not man.get("companions"):
        return {"agency": ag, "note": "0 rows"}
    eff = effective_date or _effective_date(ag, cdate)
    stats, collapsed = ingest_edition(store, rows, eff, f"gh {sha[:9]}",
                                      source=CANON_SOURCE, source_commit=sha,
                                      commit_date=cdate, complete=None)
    out = {"agency": ag, "sha": sha[:9], "effective_date": eff, "chunks": len(rows),
           "new": stats["new"], "changed": stats["changed"], "collapsed": len(collapsed)}
    if companion_store is not None:
        from chunker.ingest import companion as _fcomp
        cstats, _cc = _fcomp.route(companion_store, man.get("companions", []), ag, eff,
                                   source_commit=sha, commit_date=cdate)
        companion_store.save()
        out["companion_new"] = cstats.get("new", 0)
        out["companion_rows"] = cstats.get("snapshot_rows", 0)
    store.save()
    return out
