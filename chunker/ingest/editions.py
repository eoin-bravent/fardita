"""Edition-source strategies — "given a repo, what are the published editions + dates?"

Three strategies behind one interface (`.plan(repo, agency) -> [Edition]`):

  FacRevSource   FAR: group first-parent commits by the ditamap rev="FAC ...", settled
                 commit = last of each FAC group, effective date parsed from the rev.
                 (Port of pipeline/update.py `_edition_plan`.)
  MarkerSource   non-FAR: group first-parent dita-touching commits by their BRANCH MARKER
                 (from the merge subject), settled = last of each group. (Port of
                 pipeline/canon.py `edition_plan` + `_parse_edition`, WITH the resolved-rule
                 fix — see below.)
  HeadSource     just the current HEAD as one edition (the ongoing-update default / fallback).

Resolved GSA-maintainer rule (chunker-edition-boundaries): effective_from is the date in
the branch marker; when the marker carries no date, the git commit date is an acceptable
stand-in ONLY for FAR / DFARS / GSAM(GSAR) — for every other agency the date is flagged
UNKNOWN (effective_date=None) rather than guessed. That is the ONE behavioral change vs the
old canon.py (which fell back to the commit date for everyone). Steven_*/Sandbox_* (and
testing/scratch/temporary-redact) commits are flagged junk. Consecutive same-marker commits
collapse to one edition (the last). An Edition is:

    {label, effective_date|None, settled_sha, commit_date, first_sha, n_commits, junk}
"""
import os
import re
import subprocess

from chunker import paths

# ---- git ----------------------------------------------------------------------
def _git(repo, *args, ok_fail=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        if ok_fail:
            return None
        raise RuntimeError(f"git {' '.join(args)}: {(p.stderr or '').strip()[:200]}")
    return p.stdout


def _default_branch(repo):
    out = _git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD", ok_fail=True)
    return out.strip() if out else "HEAD"


def _dita_rel(repo):
    return "dita" if os.path.isdir(os.path.join(repo, "dita")) else ""


def _commit_date(repo, sha):
    return (_git(repo, "log", "-1", "--format=%cs", sha, ok_fail=True) or "").strip()


# ---- FAR: ditamap FAC rev (port of update.py) ---------------------------------
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DITAMAP_REL = "dita/FAR.ditamap"


def parse_effective_date(rev):
    """'FAC 2026-01 March 13, 2026' -> '2026-03-13'; None when unparseable ('August XX')."""
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", rev or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"


def fac_id(rev):
    m = re.search(r"FAC\s+(\d{4}-\d+)", rev or "")
    return m.group(1) if m else None


def rev_at_commit(repo, sha, ditamap_rel=DITAMAP_REL):
    raw = _git(repo, "show", f"{sha}:{ditamap_rel}", ok_fail=True)
    if raw is None:
        return None
    m = re.search(r'<map\b[^>]*\brev="([^"]*)"', raw)
    return m.group(1).strip() if m else None


def _far_edition_plan(repo, branch, ditamap_rel=DITAMAP_REL):
    """Port of update.py._edition_plan: [{fac, start, settled_idx, settled_sha, first_sha,
    rev}] over first-parent history, settled = last commit before the next FAC's rev."""
    fp = _git(repo, "log", "--first-parent", "--reverse", "--format=%H", branch).split()
    touched = _git(repo, "log", "--first-parent", "--reverse", "--format=%H", branch,
                   "--", ditamap_rel).split()
    touched_set = set(touched)
    eras, cur = [], None
    for i, sha in enumerate(fp):
        if sha in touched_set:
            rev = rev_at_commit(repo, sha, ditamap_rel)
            f = fac_id(rev) or (rev or "?")
            if f != cur:
                eras.append({"fac": f, "start": i})
                cur = f
    for j, e in enumerate(eras):
        e["settled_idx"] = eras[j + 1]["start"] - 1 if j + 1 < len(eras) else len(fp) - 1
        e["settled_sha"] = fp[e["settled_idx"]]
        e["first_sha"] = fp[e["start"]]
        e["rev"] = rev_at_commit(repo, e["settled_sha"], ditamap_rel) or ""
    return eras, fp


class FacRevSource:
    kind = "fac-rev"

    def plan(self, repo, agency="FAR"):
        eras, _fp = _far_edition_plan(repo, _default_branch(repo))
        out = []
        for e in eras:
            out.append({
                "label": e["rev"] or e["fac"],
                "fac": e["fac"],
                "effective_date": parse_effective_date(e["rev"]),   # None if interim/unparseable
                "settled_sha": e["settled_sha"],
                "commit_date": _commit_date(repo, e["settled_sha"]),
                "first_sha": e["first_sha"],
                "n_commits": e["settled_idx"] - e["start"] + 1,
                "junk": not re.match(r"^\d{4}-\d+$", e["fac"] or ""),  # non-FAC boundary = bootstrap
            })
        return out


# ---- non-FAR: branch marker (port of canon.py, WITH the fix) ------------------
# commits that are NOT real regulation editions (test/sandbox/tooling)
_EDITION_JUNK = re.compile(r"(?i)sandbox|steven|testing|temporary.?redact|scratch|\btest\b")
_MONTHS3 = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

# agencies for which the git commit date is an acceptable effective-date fallback when the
# marker has no date (GSA maintainers). Everyone else -> effective_date None (unknown).
COMMIT_DATE_FALLBACK = {"FAR", "DFARS", "DFARSPGI", "GSAM", "GSAR"}


def _parse_edition(subject):
    """From a first-parent merge subject/branch name -> (label, effective_iso|None, is_junk)."""
    s = re.sub(r"(?i)^merge (pull request #\d+ from \S+?/|branch .)", "", subject)
    s = s.strip().strip("'\"")
    junk = bool(_EDITION_JUNK.search(s))
    m = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", s)              # YYYYMMDD
    if m and 1 <= int(m[2]) <= 12 and 1 <= int(m[3]) <= 31:
        return s, f"{m[1]}-{m[2]}-{m[3]}", junk
    m = re.search(r"(?<!\d)(\d{1,2})[-/](\d{1,2})[-/](20\d{2}|\d{2})(?!\d)", s)  # M-D-YY(YY)
    if m:
        mo, d, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return s, f"{y:04d}-{mo:02d}-{d:02d}", junk
    m = re.search(r"(?i)\b([a-z]{3,9})[ _-](20\d{2})\b", s)                # Month YYYY (day unknown)
    if m and m[1][:3].lower() in _MONTHS3:
        return s, None, junk
    return s, None, junk


def marker_edition_plan(repo, rel, commit_date_ok):
    """Port of canon.py.edition_plan WITH the resolved-rule fix: fall back to the settled
    commit date ONLY when commit_date_ok (FAR/DFARS/GSAM); else leave effective_date=None."""
    if not rel:                       # no dita/ in the checkout -> no marker editions to read;
        return []                     # avoids the fatal `git log -- ''` pathspec error and lets
                                      # the pass fall back cleanly (ingest_head still gets HEAD).
    log = _git(repo, "log", "--first-parent", "--reverse", "--format=%H%x1f%cs%x1f%s",
               "--", rel) or ""
    commits = []
    for ln in log.splitlines():
        p = ln.split("\x1f")
        if len(p) == 3:
            label, eff, junk = _parse_edition(p[2])
            commits.append({"sha": p[0], "cdate": p[1], "subj": p[2],
                            "label": label, "eff": eff, "junk": junk,
                            "key": eff or label})
    groups = []
    for c in commits:
        if groups and groups[-1][-1]["key"] == c["key"]:
            groups[-1].append(c)
        else:
            groups.append([c])
    out = []
    for g in groups:
        settled, first = g[-1], g[0]
        eff = next((c["eff"] for c in reversed(g) if c["eff"]), None)
        if eff is None and commit_date_ok:      # <-- the fix (was: `or settled['cdate']` for all)
            eff = settled["cdate"]
        out.append({"label": settled["label"], "effective_date": eff,   # may be None (UNKNOWN)
                    "settled_sha": settled["sha"], "commit_date": settled["cdate"],
                    "first_sha": first["sha"], "n_commits": len(g),
                    "junk": all(c["junk"] for c in g)})
    return out


class MarkerSource:
    kind = "marker"

    def plan(self, repo, agency):
        rel = _dita_rel(repo)
        ok = agency.upper() in COMMIT_DATE_FALLBACK
        return marker_edition_plan(repo, rel, commit_date_ok=ok)


class HeadSource:
    kind = "head"

    def plan(self, repo, agency=""):
        sha = (_git(repo, "rev-parse", "HEAD", ok_fail=True) or "").strip()
        cdate = _commit_date(repo, sha)
        return [{"label": "HEAD", "effective_date": cdate, "settled_sha": sha,
                 "commit_date": cdate, "first_sha": sha, "n_commits": 1, "junk": False}]


_SOURCES = {"fac-rev": FacRevSource, "marker": MarkerSource, "head": HeadSource}


def source_for(agency):
    """Pick the edition source for an agency: agencies.json `edition_source` override, else
    FAR -> fac-rev, everyone else -> marker (Decision B resolved)."""
    kind = (paths.agencies_cfg().get(agency) or {}).get("edition_source")
    if not kind:
        kind = "fac-rev" if agency == "FAR" else "marker"
    return _SOURCES[kind]()
