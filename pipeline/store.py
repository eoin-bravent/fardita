#!/usr/bin/env python3
"""Versioned chunk store (SCD Type 2) + snapshot merge engine.

The store holds every version of every chunk, each row = one chunk version:

    identity      (citation, alternate)          -- stable across editions
    version key   (citation, alternate, effective_from)
    temporal      effective_from / effective_to  -- LEGAL effective dates, half-open
                  [effective_from, effective_to); effective_to = None => still in force
    current       True iff effective_to is None (denormalized for cheap filtering)
    provenance    source ('gsa-github'|'acquisition-gov-archive'|'manual'),
                  source_commit, ingested_at, source_version, pipeline_version
    change detect content_hash over HASH_FIELDS (text + structure + titles + clause meta;
                  deliberately EXCLUDES changes[] -- rev marks drop out each FAC and would
                  fabricate versions -- and cross/external refs, which derive from text and
                  carry verification statuses that must survive unchanged chunks)
    bookkeeping   last_seen_version / last_seen_date -- most recent ingested edition in
                  which this exact content was confirmed present

merge_snapshot() is the ONE ingestion operation: it reconciles a complete chunked snapshot
of one edition (effective on date D) into the store, and works whether D lands after,
before, or between editions already ingested -- forward daily updates and archive backfill
are the same code path.  Decision table per identity:

    in snapshot, row in force at D, same hash      -> no-op (advance last_seen)
    in snapshot, row in force at D, differs,
        row starts exactly at D                    -> ERRATA: replace content in place,
                                                      old copy appended to <REG>_errata.json
        row starts before D, next row same hash    -> extend next row backward to D
        row starts before D, otherwise             -> close row at D, insert [D, old_to)
    in snapshot, nothing in force at D:
        a later row exists, same hash              -> extend it backward to D (backfill)
        a later row exists, differs                -> insert closed row [D, later.from)
        chain empty / all rows end before D        -> insert [D, next ingested edition
                                                      after D or None)  (new / reappeared)
    in store (in force at D), absent from snapshot:
        row starts exactly at D                    -> errata removal (row deleted, logged)
        seen in an edition after D (backfill gap)  -> split: close at D, reopen at the next
                                                      ingested edition after D
        otherwise                                  -> close at D (section removed)

All dates are 'YYYY-MM-DD' strings (ISO order == lexicographic order).  No third-party deps.
"""
import os
import json
import hashlib
import datetime
from collections import defaultdict

FORMAT = 1

HASH_FIELDS = ["text", "type", "instrument", "part_title", "subpart_title",
               "section_title", "subsection_title", "date", "prescribed_by",
               "reserved", "end_marker", "images"]

# content fields replaced wholesale on an errata replacement (temporal fields never move)
CONTENT_FIELDS = HASH_FIELDS + ["changes", "cross_references", "external_references",
                                "url", "source_version", "pipeline_version"]

STAT_KEYS = ["snapshot_rows", "identities_in_snapshot", "unchanged", "new", "changed",
             "closed", "reopened", "backfilled", "extended_backward", "gap_split",
             "errata_replaced", "errata_removed", "duplicates_in_snapshot"]


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(row):
    basis = {k: row.get(k) for k in HASH_FIELDS}
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def identity(row):
    return (row["citation"], row.get("alternate", ""))


def in_force(row, d):
    return row["effective_from"] <= d and (row["effective_to"] is None or d < row["effective_to"])


def section_of(row):
    """Bare section number for LSA comparison: 'FAR-22.1503(b)(2)' -> '22.1503'."""
    head = row["citation"].split("(")[0]
    prefix = f'{row.get("regulation", "")}-'
    return head[len(prefix):] if head.startswith(prefix) else head


def _atomic_dump(obj, path, indent=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


class Store:
    """Load/merge/save the versioned store for one regulation."""

    def __init__(self, store_dir, regulation="FAR"):
        self.dir = os.path.abspath(store_dir)
        self.regulation = regulation
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, f"{regulation}_store.json")
        self.errata_path = os.path.join(self.dir, f"{regulation}_errata.json")
        self.state_path = os.path.join(self.dir, "state.json")
        if os.path.exists(self.path):
            self.data = json.load(open(self.path, encoding="utf-8"))
        else:
            self.data = {"format": FORMAT, "regulation": regulation, "editions": [], "rows": []}

    # ---------- accessors ----------
    @property
    def rows(self):
        return self.data["rows"]

    @property
    def editions(self):
        return self.data["editions"]

    def chains(self):
        c = defaultdict(list)
        for r in self.rows:
            c[identity(r)].append(r)
        for rows in c.values():
            rows.sort(key=lambda r: r["effective_from"])
        return c

    def as_of(self, date, citation=None, alternate=None):
        """Rows legally in force on `date` (optionally filtered to one identity)."""
        out = [r for r in self.rows if in_force(r, date)]
        if citation is not None:
            out = [r for r in out if r["citation"] == citation]
        if alternate is not None:
            out = [r for r in out if r.get("alternate", "") == alternate]
        return out

    def current_rows(self):
        return [r for r in self.rows if r["effective_to"] is None]

    # ---------- persistence ----------
    def save(self):
        self.rows.sort(key=lambda r: (r["citation"], r.get("alternate", ""), r["effective_from"]))
        self.editions.sort(key=lambda e: e["effective_date"])
        _atomic_dump(self.data, self.path)

    def load_state(self):
        if os.path.exists(self.state_path):
            return json.load(open(self.state_path, encoding="utf-8"))
        return {}

    def save_state(self, source, entry):
        state = self.load_state()
        state[source] = entry
        _atomic_dump(state, self.state_path, indent=2)

    def _errata_append(self, items):
        log = (json.load(open(self.errata_path, encoding="utf-8"))
               if os.path.exists(self.errata_path) else [])
        log.extend(items)
        _atomic_dump(log, self.errata_path, indent=2)

    # ---------- the merge engine ----------
    def merge_snapshot(self, snap_rows, effective_date, source_version,
                       source="gsa-github", source_commit="", complete=True,
                       ingested_at=None):
        """Reconcile one complete edition snapshot into the store.  Returns stats dict.

        complete=False (future partial/manual ingests) skips absence handling, i.e. never
        closes or removes rows just because they are missing from the snapshot."""
        D = effective_date
        now = ingested_at or utcnow()
        stats = {k: 0 for k in STAT_KEYS}
        changed_sections = set()
        errata_items = []
        new_rows = []
        remove_ids = set()

        # dedupe snapshot on identity (defensive; the chunker shouldn't emit duplicates)
        snap = {}
        for r in snap_rows:
            k = identity(r)
            if k in snap:
                stats["duplicates_in_snapshot"] += 1
                continue
            snap[k] = r
        stats["snapshot_rows"] = len(snap_rows)
        stats["identities_in_snapshot"] = len(snap)

        chains = self.chains()
        edition_dates = sorted({e["effective_date"] for e in self.editions})

        def next_edition_after(d):
            return next((x for x in edition_dates if x > d), None)

        def make_row(src_chunk, ef, et):
            r = dict(src_chunk)
            r["content_hash"] = content_hash(src_chunk)
            r["effective_from"] = ef
            r["effective_to"] = et
            r["current"] = et is None
            r["ingested_at"] = now
            r["source"] = source
            r["source_commit"] = source_commit
            r["last_seen_version"] = source_version
            r["last_seen_date"] = D
            return r

        def bump_seen(row):
            if D >= row.get("last_seen_date", ""):
                row["last_seen_date"] = D
                row["last_seen_version"] = source_version

        for k in set(chains) | set(snap):
            chain = chains.get(k, [])
            s = snap.get(k)
            r_at = next((r for r in chain if in_force(r, D)), None)

            if s is not None:
                h = content_hash(s)
                if r_at is not None:
                    if r_at["content_hash"] == h:
                        bump_seen(r_at)
                        stats["unchanged"] += 1
                    elif r_at["effective_from"] == D:
                        # errata: same legal version, corrected content -- replace in place
                        errata_items.append({
                            "action": "replaced", "replaced_at": now,
                            "new_source_commit": source_commit,
                            "new_source_version": source_version,
                            "new_content_hash": h, "row": dict(r_at)})
                        for f in CONTENT_FIELDS:
                            if f in s:
                                r_at[f] = s[f]
                        r_at["content_hash"] = h
                        r_at["ingested_at"] = now
                        r_at["source"] = source
                        r_at["source_commit"] = source_commit
                        # the replacement brought parser-only refs for CHANGED text, so any
                        # prior verification no longer applies -- clear the stamp so the
                        # unit re-queues for audit (the superseded row, with its verified
                        # refs intact, is preserved in the errata log above)
                        r_at.pop("refs_verified_from", None)
                        bump_seen(r_at)
                        stats["errata_replaced"] += 1
                        changed_sections.add(section_of(s))
                    else:
                        i = chain.index(r_at)
                        nxt = chain[i + 1] if i + 1 < len(chain) else None
                        if nxt is not None and nxt["content_hash"] == h \
                                and nxt["effective_from"] == r_at["effective_to"]:
                            # the text at D equals the already-known NEXT version:
                            # that version simply started earlier than we knew
                            r_at["effective_to"] = D
                            nxt["effective_from"] = D
                            stats["extended_backward"] += 1
                        else:
                            old_to = r_at["effective_to"]
                            r_at["effective_to"] = D
                            new_rows.append(make_row(s, D, old_to))
                            stats["changed"] += 1
                            changed_sections.add(section_of(s))
                else:
                    later = next((r for r in chain if r["effective_from"] > D), None)
                    if later is not None:
                        if later["content_hash"] == h:
                            later["effective_from"] = D          # backfill: push the floor down
                            stats["extended_backward"] += 1
                        else:
                            new_rows.append(make_row(s, D, later["effective_from"]))
                            stats["backfilled"] += 1
                    else:
                        et = next_edition_after(D)   # absent from the next ingested edition (if any)
                        new_rows.append(make_row(s, D, et))
                        if chain:
                            stats["reopened"] += 1
                        else:
                            stats["new"] += 1
                        changed_sections.add(section_of(s))
            else:
                # identity in store, absent from this (complete) snapshot
                if not complete or r_at is None:
                    continue
                if r_at["effective_from"] == D:
                    # this same edition previously produced the row; the re-ingest dropped it
                    errata_items.append({
                        "action": "removed", "replaced_at": now,
                        "new_source_commit": source_commit,
                        "new_source_version": source_version,
                        "new_content_hash": None, "row": dict(r_at)})
                    remove_ids.add(id(r_at))
                    stats["errata_removed"] += 1
                    changed_sections.add(section_of(r_at))
                elif r_at.get("last_seen_date", "") > D:
                    # backfill gap: absent at D but confirmed present in a later edition
                    net = next_edition_after(D)
                    old_to = r_at["effective_to"]
                    r_at["effective_to"] = D
                    reopened = dict(r_at)
                    reopened["effective_from"] = net
                    reopened["effective_to"] = old_to
                    reopened["ingested_at"] = now
                    new_rows.append(reopened)
                    stats["gap_split"] += 1
                    changed_sections.add(section_of(r_at))
                else:
                    r_at["effective_to"] = D                     # removed as of this edition
                    stats["closed"] += 1
                    changed_sections.add(section_of(r_at))

        if remove_ids:
            self.data["rows"] = [r for r in self.rows if id(r) not in remove_ids]
        self.rows.extend(new_rows)
        for r in self.rows:                                      # current == open interval, always
            r["current"] = r["effective_to"] is None

        # edition registry (one entry per effective date; re-ingest updates it)
        entry = {"effective_date": D, "source_version": source_version, "source": source,
                 "source_commit": source_commit, "ingested_at": now,
                 "stats": {k: stats[k] for k in STAT_KEYS}}
        existing = next((e for e in self.editions if e["effective_date"] == D), None)
        if existing:
            entry["first_ingested_at"] = existing.get("first_ingested_at",
                                                      existing.get("ingested_at"))
            existing.update(entry)
        else:
            entry["first_ingested_at"] = now
            self.editions.append(entry)

        if errata_items:
            self._errata_append(errata_items)

        stats["sections_changed"] = sorted(changed_sections)
        return stats

    # ---------- invariants ----------
    def verify(self):
        """Return a list of invariant violations (empty = healthy)."""
        problems = []
        for k, chain in sorted(self.chains().items()):
            key = f"{k[0]}|alt={k[1]}" if k[1] else k[0]
            open_rows = [r for r in chain if r["effective_to"] is None]
            if len(open_rows) > 1:
                problems.append(f"{key}: {len(open_rows)} open rows (must be 0 or 1)")
            for r in chain:
                if r["effective_to"] is not None and r["effective_to"] <= r["effective_from"]:
                    problems.append(f"{key}: empty/negative interval "
                                    f"[{r['effective_from']}, {r['effective_to']})")
                if r["current"] != (r["effective_to"] is None):
                    problems.append(f"{key}: current flag inconsistent at {r['effective_from']}")
            for a, b in zip(chain, chain[1:]):
                if a["effective_to"] is None:
                    problems.append(f"{key}: open row at {a['effective_from']} is not last")
                elif a["effective_to"] > b["effective_from"]:
                    problems.append(f"{key}: overlap [{a['effective_from']},{a['effective_to']}) "
                                    f"vs [{b['effective_from']},…)")
                if a["effective_from"] == b["effective_from"]:
                    problems.append(f"{key}: duplicate effective_from {a['effective_from']}")
        return problems
