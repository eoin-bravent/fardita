#!/usr/bin/env python3
"""Dependency-free port of the acquisition.gov archive downloader.

The old_fardita/download_acquisition_archives_v3.py needed requests + beautifulsoup4 + dateutil
+ tqdm. This keeps the chunker lean: stdlib urllib/zipfile for HTTP + zip, and lxml (already a
chunker dependency, used by the parsers) for the archives-table HTML parse. No new dependencies.

It scrapes acquisition.gov/archives?type=<TYPE>, downloads + extracts every published edition ZIP
into paths.archive_dir(agency), and writes archive_metadata.json in the {folder_name, number,
effective_date} shape chunker.parsers._adapter.load_archive_dates consumes.

Idempotent: a ZIP already on disk + valid is skipped; a folder already extracted (marked by
.extraction_complete) is skipped -- so a re-run fetches only NEW editions. Combined with the
idempotent archive backfill, this is the acquisition.gov UPDATE path for content GitHub does not
carry (chiefly DFARSPGI, whose PGI content is published to acquisition.gov, not the DFARS repo).
"""
import os
import re
import sys
import time
import json
import shutil
import zipfile
import urllib.request
import urllib.error
from urllib.parse import urlencode, urljoin, urlsplit, unquote

import lxml.html

from chunker import paths

ARCHIVES_URL = "https://www.acquisition.gov/archives"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "chunker-archive-downloader/1.0")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def archive_type(agency):
    """The acquisition.gov ?type= value: agencies.json `archive_type` override, else the agency."""
    return (paths.agencies_cfg().get(agency) or {}).get("archive_type") or agency


def _safe_component(value, fallback):
    cleaned = _BAD.sub("_", value).rstrip(" .")
    return cleaned or fallback


def _parse_date(raw):
    """An archives-table effective-date cell -> ISO 'YYYY-MM-DD'. Handles 'March 17, 2025',
    '3/17/2025', '2025-03-17', '2025-0317'. '' if unparseable (the caller skips that row)."""
    s = " ".join((raw or "").split())
    m = re.search(r"(?<!\d)(20\d{2})-?(\d{2})-?(\d{2})(?!\d)", s)           # ISO / YYYY-MMDD
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", s)         # Month D, YYYY
    if m and m.group(1)[:3].lower() in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1)[:3].lower()]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](20\d{2})(?!\d)", s)   # M/D/YYYY
    if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _http_text(url, retries=5, timeout=120):
    """GET a page as text, with backoff on 429/5xx + transient network errors."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(2 ** attempt); continue
            raise


def _page_url(atype, page):
    return f"{ARCHIVES_URL}?{urlencode({'type': atype, 'page': page})}"


def _norm_url(url):
    """Old public links use login.acquisition.gov -> www.acquisition.gov."""
    if (urlsplit(url).hostname or "").lower() == "login.acquisition.gov":
        return url.replace("login.acquisition.gov", "www.acquisition.gov", 1)
    return url


def _find_table(root):
    """The archive results table: thead carries Number / Effective Date / Zip File columns.
    Returns (table_element, {'number'|'date'|'zip': col_index}) or (None, None)."""
    for table in root.iter("table"):
        heads = [" ".join(th.text_content().split()).lower()
                 for th in table.xpath(".//thead//th | .//thead//td")]
        idx = {}
        for i, h in enumerate(heads):
            if h.startswith("number"):
                idx["number"] = i
            elif h.startswith("effective date"):
                idx["date"] = i
            elif h.startswith("zip file"):
                idx["zip"] = i
        if {"number", "date", "zip"} <= idx.keys():
            return table, idx
    return None, None


def _records_from_page(root, page_url):
    table, idx = _find_table(root)
    if table is None:
        return []
    hi = max(idx.values())
    out = []
    for row in table.xpath(".//tbody/tr"):
        cells = row.xpath("./th | ./td")
        if len(cells) <= hi:
            continue
        number = " ".join(cells[idx["number"]].text_content().split())
        date = _parse_date(cells[idx["date"]].text_content())
        if not number or not date:
            continue
        zip_url = None
        for a in cells[idx["zip"]].xpath(".//a[@href]"):
            cand = _norm_url(urljoin(page_url, a.get("href")))
            if urlsplit(cand).path.lower().endswith(".zip"):
                zip_url = cand
                break
        if not zip_url:
            continue
        raw = unquote(os.path.basename(urlsplit(zip_url).path))
        zip_name = _safe_component(raw, "archive.zip")
        if not zip_name.lower().endswith(".zip"):
            zip_name += ".zip"
        out.append({"number": number, "effective_date": date, "zip_url": zip_url,
                    "zip_filename": zip_name,
                    "folder_name": _safe_component(os.path.splitext(zip_name)[0], "extracted")})
    return out


def _last_page(root, atype):
    """Largest zero-based page in the pagination links for this archive type."""
    pages = {0}
    for a in root.xpath("//a[@href]"):
        p = urlsplit(urljoin(ARCHIVES_URL, a.get("href")))
        if p.path.rstrip("/") != "/archives":
            continue
        q = dict(pair.split("=", 1) for pair in p.query.split("&") if "=" in pair)
        if q.get("type", "").upper() not in ("", atype.upper()):
            continue
        try:
            pages.add(int(unquote(q.get("page", "0"))))
        except ValueError:
            pass
    return max(pages)


def collect_records(atype, progress=True):
    """Every downloadable edition across all pages, deduped by ZIP url with unique local folder
    names (deterministic; Windows-safe)."""
    first = _page_url(atype, 0)
    root = lxml.html.fromstring(_http_text(first))
    last = _last_page(root, atype)
    recs = _records_from_page(root, first)
    if progress:
        print(f"  [archives:{atype}] pages 0..{last}; page 0: {len(recs)} zip(s)")
    for pg in range(1, last + 1):
        url = _page_url(atype, pg)
        recs.extend(_records_from_page(lxml.html.fromstring(_http_text(url)), url))
    seen, uniq = set(), []
    for rec in recs:                                        # drop exact-duplicate urls (sort/paging quirks)
        if rec["zip_url"] not in seen:
            seen.add(rec["zip_url"])
            uniq.append(rec)
    used = set()                                            # disambiguate colliding local folder names
    for rec in uniq:
        base, fn = rec["folder_name"], rec["folder_name"]
        if fn.casefold() in used:
            fn = _safe_component(f"{base}__{rec['number']}_{rec['effective_date']}", base)
            n = 2
            while fn.casefold() in used:
                fn = f"{base}__{rec['number']}_{n}"
                n += 1
            rec["folder_name"] = fn
        used.add(rec["folder_name"].casefold())
    return uniq


def _download_zip(url, dest, retries=5, timeout=300):
    """Stream a ZIP to `dest` (skip if already present + valid). Validates it is a real ZIP."""
    if os.path.exists(dest) and zipfile.is_zipfile(dest):
        return False
    part = dest + ".part"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, length=1024 * 1024)
            break
        except urllib.error.HTTPError as e:
            if os.path.exists(part):
                os.remove(part)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if os.path.exists(part):
                os.remove(part)
            if attempt < retries - 1:
                time.sleep(2 ** attempt); continue
            raise
    if not zipfile.is_zipfile(part):
        os.remove(part)
        raise zipfile.BadZipFile(f"downloaded response is not a ZIP: {url}")
    os.replace(part, dest)
    return True


def _safe_extract(zip_path, dest):
    """Extract with path-traversal + symlink guards (no member escapes `dest`)."""
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            name = m.filename
            parts = name.replace("\\", "/").split("/")
            if name.startswith("/") or ".." in parts:
                raise ValueError(f"unsafe path in {os.path.basename(zip_path)}: {name}")
            if (m.external_attr >> 16) & 0o170000 == 0o120000:      # S_IFLNK
                raise ValueError(f"symlink rejected in {os.path.basename(zip_path)}: {name}")
            target = os.path.realpath(os.path.join(dest, *parts))
            if os.path.commonpath([root, target]) != root:
                raise ValueError(f"unsafe path in {os.path.basename(zip_path)}: {name}")
        z.extractall(dest)


def _extract(zip_path, folder_name, agency_dir):
    """Extract into agency_dir/folder_name, atomically, skipping if already complete."""
    dest = os.path.join(agency_dir, folder_name)
    marker = os.path.join(dest, ".extraction_complete")
    if os.path.exists(marker):
        return False
    tmp = os.path.join(agency_dir, f".{folder_name}.extracting")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    _safe_extract(zip_path, tmp)
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(os.path.basename(zip_path) + "\n")
    return True


def _write_metadata(records, path):
    """archive_metadata.json in load_archive_dates()'s shape; written atomically."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([{"number": r["number"], "effective_date": r["effective_date"],
                    "folder_name": r["folder_name"]} for r in records],
                  f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def download_archives(agency, *, overwrite=False, delete_zips=False, progress=True):
    """Scrape + download + extract every archive edition for one agency into paths.archive_dir,
    writing archive_metadata.json. Idempotent (skips ZIPs/folders already present unless
    overwrite). Returns a summary dict."""
    atype = archive_type(agency)
    adir = paths.archive_dir(agency)
    zdir = os.path.join(adir, "_zips")
    os.makedirs(zdir, exist_ok=True)
    meta_path = os.path.join(adir, "archive_metadata.json")

    records = collect_records(atype, progress=progress)
    if progress:
        print(f"  [archives:{agency}] {len(records)} edition zip(s) -> {adir}")
    done, new, failed = [], 0, []
    for rec in records:
        dest = os.path.join(adir, rec["folder_name"])
        zp = os.path.join(zdir, rec["zip_filename"])
        if overwrite:
            shutil.rmtree(dest, ignore_errors=True)
            if os.path.exists(zp):
                os.remove(zp)
        try:
            _download_zip(rec["zip_url"], zp)
            if _extract(zp, rec["folder_name"], adir):
                new += 1
            done.append(rec)
            _write_metadata(done, meta_path)               # keep the JSON valid mid-run
            if delete_zips and os.path.exists(zp):
                os.remove(zp)
        except Exception as e:
            failed.append((rec["number"], repr(e)))
            if progress:
                print(f"    [{agency}] FAILED {rec['number']}: {e}", file=sys.stderr)
    _write_metadata(done, meta_path)
    if progress:
        print(f"  [archives:{agency}] extracted {len(done)} ({new} new), {len(failed)} failed")
    return {"agency": agency, "type": atype, "editions": len(records),
            "extracted": len(done), "new": new, "failed": failed, "dir": adir}
