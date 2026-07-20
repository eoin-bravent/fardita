# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "beautifulsoup4>=4.12",
#   "python-dateutil>=2.9",
#   "requests>=2.32",
#   "tqdm>=4.67",
# ]
# ///

"""
Download and extract every ZIP archive for one Acquisition.gov regulation
archive type, and create JSON metadata for the extracted folders.

Examples:
    uv run download_acquisition_archives.py --agency AFARS
    uv run download_acquisition_archives.py --agency FAR --output C:\\archive
    uv run download_acquisition_archives.py --agency DFARS --delete-zips

Default layout:
    archive/
      AFARS/
        _zips/
        2025-0317_HTML_Files/
        2025-0314_HTML_Files/
        archive_metadata.json

JSON format:
    [
      {
        "number": "2025-0317",
        "effective_date": "2025-03-17",
        "folder_name": "2025-0317_HTML_Files"
      }
    ]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import (
    parse_qs,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import requests
from bs4 import BeautifulSoup, Tag
from dateutil.parser import parse as parse_date
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


SCRIPT_VERSION = "3.0.0"
ARCHIVES_URL = "https://www.acquisition.gov/archives"
INVALID_WINDOWS_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class ArchiveRecord:
    number: str
    effective_date: str
    zip_url: str
    zip_filename: str
    folder_name: str
    alternate_zip_urls: tuple[str, ...] = ()

    @property
    def download_urls(self) -> tuple[str, ...]:
        """Primary URL followed by any legacy/mirror URLs for the same ZIP."""
        return (self.zip_url, *self.alternate_zip_urls)

    def metadata(self) -> dict[str, str]:
        return {
            "number": self.number,
            "effective_date": self.effective_date,
            "folder_name": self.folder_name,
        }


def build_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Acquisition-archive-downloader/2.0"
            )
        }
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split()).strip()


def safe_path_component(value: str, fallback: str) -> str:
    cleaned = INVALID_WINDOWS_FILENAME_CHARS.sub("_", value).rstrip(" .")
    return cleaned or fallback


def archive_page_url(agency: str, page: int) -> str:
    return f"{ARCHIVES_URL}?{urlencode({'type': agency, 'page': page})}"


def normalize_download_url(url: str) -> str:
    """Normalize old public links that use login.acquisition.gov."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() == "login.acquisition.gov":
        netloc = "www.acquisition.gov"
        if parts.port:
            netloc += f":{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def fetch_soup(session: requests.Session, url: str) -> BeautifulSoup:
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def find_archive_table(soup: BeautifulSoup) -> Tag:
    for table in soup.find_all("table"):
        headings = {
            normalize_whitespace(cell.get_text(" ", strip=True)).lower()
            for cell in table.select("thead th, thead td")
        }
        if (
            any(h.startswith("number") for h in headings)
            and any(h.startswith("effective date") for h in headings)
            and any(h.startswith("zip file") for h in headings)
        ):
            return table
    raise ValueError("Could not find the archive results table.")


def find_column_indices(table: Tag) -> dict[str, int]:
    header_row = table.select_one("thead tr")
    if header_row is None:
        raise ValueError("The archive table has no header row.")

    indices: dict[str, int] = {}
    for index, header in enumerate(
        header_row.find_all(["th", "td"], recursive=False)
    ):
        heading = normalize_whitespace(header.get_text(" ", strip=True)).lower()
        if heading.startswith("number"):
            indices["number"] = index
        elif heading.startswith("effective date"):
            indices["effective_date"] = index
        elif heading.startswith("zip file"):
            indices["zip_file"] = index

    missing = {"number", "effective_date", "zip_file"} - indices.keys()
    if missing:
        raise ValueError(
            "Missing required archive table column(s): "
            + ", ".join(sorted(missing))
        )
    return indices


def parse_effective_date(raw_date: str) -> str:
    try:
        return parse_date(raw_date, fuzzy=True).date().isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Could not parse effective date {raw_date!r}.") from exc


def zip_filename_from_url(url: str) -> str:
    raw_name = unquote(PurePosixPath(urlsplit(url).path).name)
    safe_name = safe_path_component(raw_name, "archive.zip")
    if not safe_name.lower().endswith(".zip"):
        safe_name += ".zip"
    return safe_name


def parse_records_from_page(
    soup: BeautifulSoup,
    page_url: str,
) -> tuple[list[ArchiveRecord], list[tuple[str, str]]]:
    table = find_archive_table(soup)
    indices = find_column_indices(table)
    highest_index = max(indices.values())
    records: list[ArchiveRecord] = []
    rows_without_zips: list[tuple[str, str]] = []

    for row in table.select("tbody tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) <= highest_index:
            continue

        number = normalize_whitespace(
            cells[indices["number"]].get_text(" ", strip=True)
        )
        raw_date = normalize_whitespace(
            cells[indices["effective_date"]].get_text(" ", strip=True)
        )
        if not number or not raw_date:
            continue

        effective_date = parse_effective_date(raw_date)
        zip_url: str | None = None

        for link in cells[indices["zip_file"]].select("a[href]"):
            candidate = normalize_download_url(urljoin(page_url, link["href"]))
            if urlsplit(candidate).path.lower().endswith(".zip"):
                zip_url = candidate
                break

        if zip_url is None:
            rows_without_zips.append((number, effective_date))
            continue

        zip_filename = zip_filename_from_url(zip_url)
        records.append(
            ArchiveRecord(
                number=number,
                effective_date=effective_date,
                zip_url=zip_url,
                zip_filename=zip_filename,
                folder_name=safe_path_component(
                    Path(zip_filename).stem,
                    "extracted",
                ),
            )
        )

    return records, rows_without_zips


def discover_last_page(soup: BeautifulSoup, agency: str) -> int:
    """Return the largest zero-based page number in the pagination links."""
    page_numbers = {0}
    for link in soup.select("a[href]"):
        absolute = urljoin(ARCHIVES_URL, link["href"])
        parts = urlsplit(absolute)
        if parts.path.rstrip("/") != "/archives":
            continue

        query = parse_qs(parts.query)
        types = query.get("type", [])
        if types and types[0].upper() != agency.upper():
            continue

        for raw_page in query.get("page", []):
            try:
                page_numbers.add(int(raw_page))
            except ValueError:
                pass
    return max(page_numbers)


def url_preference(url: str, agency: str) -> tuple[int, int]:
    """Prefer agency-specific archive paths over older generic paths."""
    path = unquote(urlsplit(url).path).lower()
    score = 0

    if f"/archives/{agency.lower()}/" in path:
        score += 100
    if "/html/" in path or "/compiled_html/" in path:
        score += 20
    if "/archives/zip/" in path:
        score -= 10

    # The shorter URL wins only when the path-quality score is tied.
    return score, -len(url)


def merge_duplicate_records(
    records: list[ArchiveRecord],
    agency: str,
) -> list[ArchiveRecord]:
    """
    Merge duplicate site rows for the same logical ZIP. Acquisition.gov has
    some files reachable through both a legacy generic URL and a newer
    agency-specific URL. Keep the preferred URL and retain the others as
    download fallbacks.
    """
    grouped: dict[tuple[str, str, str], list[ArchiveRecord]] = {}
    order: list[tuple[str, str, str]] = []

    for record in records:
        key = (
            record.number.casefold(),
            record.effective_date,
            record.zip_filename.casefold(),
        )
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(record)

    merged: list[ArchiveRecord] = []

    for key in order:
        group = grouped[key]
        first = group[0]
        urls = list(
            dict.fromkeys(
                url
                for record in group
                for url in record.download_urls
            )
        )
        urls.sort(
            key=lambda url: url_preference(url, agency),
            reverse=True,
        )

        if len(urls) > 1:
            print(
                f"[duplicate] {first.number} ({first.effective_date}) has "
                f"{len(urls)} URLs; using the agency-specific URL first."
            )

        merged.append(
            replace(
                first,
                zip_url=urls[0],
                alternate_zip_urls=tuple(urls[1:]),
            )
        )

    return merged


def make_local_names_unique(
    records: list[ArchiveRecord],
) -> list[ArchiveRecord]:
    """
    Deterministically disambiguate unrelated records that nevertheless use
    the same remote basename. This avoids overwriting on Windows instead of
    aborting the entire run.
    """
    used_zip_names: set[str] = set()
    used_folder_names: set[str] = set()
    result: list[ArchiveRecord] = []

    for record in records:
        zip_filename = record.zip_filename
        folder_name = record.folder_name

        zip_collision = zip_filename.casefold() in used_zip_names
        folder_collision = folder_name.casefold() in used_folder_names

        if zip_collision or folder_collision:
            suffix = safe_path_component(
                f"{record.number}_{record.effective_date}",
                "duplicate",
            )
            zip_path = Path(zip_filename)
            base_zip = f"{zip_path.stem}__{suffix}{zip_path.suffix}"
            base_folder = f"{folder_name}__{suffix}"
            zip_filename = base_zip
            folder_name = base_folder
            counter = 2

            while (
                zip_filename.casefold() in used_zip_names
                or folder_name.casefold() in used_folder_names
            ):
                zip_filename = (
                    f"{zip_path.stem}__{suffix}_{counter}{zip_path.suffix}"
                )
                folder_name = f"{base_folder}_{counter}"
                counter += 1

            print(
                f"[rename]    Local name collision for {record.number}; "
                f"using {zip_filename!r}."
            )

        used_zip_names.add(zip_filename.casefold())
        used_folder_names.add(folder_name.casefold())
        result.append(
            replace(
                record,
                zip_filename=zip_filename,
                folder_name=folder_name,
            )
        )

    return result

def collect_records(
    session: requests.Session,
    agency: str,
) -> tuple[list[ArchiveRecord], list[tuple[str, str]]]:
    first_url = archive_page_url(agency, 0)
    print(f"[page 0] Reading {first_url}")
    first_soup = fetch_soup(session, first_url)
    last_page = discover_last_page(first_soup, agency)
    print(f"         Discovered pages 0 through {last_page}")

    records, missing = parse_records_from_page(first_soup, first_url)
    print(f"         Found {len(records)} ZIP link(s)")

    for page in range(1, last_page + 1):
        page_url = archive_page_url(agency, page)
        print(f"[page {page}] Reading {page_url}")
        soup = fetch_soup(session, page_url)
        page_records, page_missing = parse_records_from_page(soup, page_url)
        records.extend(page_records)
        missing.extend(page_missing)
        print(f"         Found {len(page_records)} ZIP link(s)")

    # First remove exact repeated URLs caused by pagination/sorting quirks.
    unique_by_url: list[ArchiveRecord] = []
    seen_urls: set[str] = set()
    for record in records:
        if record.zip_url not in seen_urls:
            seen_urls.add(record.zip_url)
            unique_by_url.append(record)

    merged = merge_duplicate_records(unique_by_url, agency)
    return make_local_names_unique(merged), missing


def download_zip(
    session: requests.Session,
    record: ArchiveRecord,
    zip_directory: Path,
) -> Path:
    zip_directory.mkdir(parents=True, exist_ok=True)
    destination = zip_directory / record.zip_filename

    if destination.exists() and zipfile.is_zipfile(destination):
        print(f"[skip]     Downloaded already: {record.zip_filename}")
        return destination

    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    errors: list[str] = []

    for attempt, url in enumerate(record.download_urls, start=1):
        if attempt == 1:
            print(f"[download] {record.zip_filename}")
        else:
            print(f"[fallback] Trying alternate URL {attempt}/{len(record.download_urls)}")

        try:
            with session.get(
                url,
                stream=True,
                timeout=(30, 300),
            ) as response:
                response.raise_for_status()
                raw_total = response.headers.get("Content-Length")
                total = (
                    int(raw_total)
                    if raw_total and raw_total.isdigit()
                    else None
                )

                with partial.open("wb") as output, tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=record.zip_filename,
                    leave=False,
                ) as progress:
                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):
                        if chunk:
                            output.write(chunk)
                            progress.update(len(chunk))

            if not zipfile.is_zipfile(partial):
                raise zipfile.BadZipFile(
                    "The downloaded response was not a valid ZIP file."
                )

            os.replace(partial, destination)
            return destination

        except Exception as exc:
            partial.unlink(missing_ok=True)
            errors.append(f"{url}: {exc}")
            if attempt < len(record.download_urls):
                print(f"[warning]  URL failed: {exc}", file=sys.stderr)

    raise RuntimeError(
        "All download URLs failed:\n  " + "\n  ".join(errors)
    )

def safe_extract(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_path = PurePosixPath(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(
                    f"Unsafe path in {zip_path.name}: {member.filename}"
                )

            unix_mode = (member.external_attr >> 16) & 0o170000
            if unix_mode == stat.S_IFLNK:
                raise ValueError(
                    f"Symbolic link rejected in {zip_path.name}: "
                    f"{member.filename}"
                )

            target = (destination / Path(*member_path.parts)).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise ValueError(
                    f"Unsafe path in {zip_path.name}: {member.filename}"
                ) from exc

        archive.extractall(destination)


def extract_zip(
    zip_path: Path,
    record: ArchiveRecord,
    agency_directory: Path,
) -> Path:
    destination = agency_directory / record.folder_name
    marker = destination / ".extraction_complete"

    if marker.exists():
        print(f"[skip]     Extracted already: {record.folder_name}")
        return destination

    temporary = agency_directory / f".{record.folder_name}.extracting"
    if temporary.exists():
        shutil.rmtree(temporary)

    print(f"[extract]  {record.zip_filename} -> {record.folder_name}")
    try:
        safe_extract(zip_path, temporary)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        marker.write_text(
            f"Extracted from {record.zip_filename}\n",
            encoding="utf-8",
        )
        return destination
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def write_metadata(records: list[ArchiveRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(
            [record.metadata() for record in records],
            file,
            indent=2,
            ensure_ascii=False,
        )
        file.write("\n")
    os.replace(temporary, output_path)


def parse_args() -> argparse.Namespace:
    script_directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Download, extract, and catalog an Acquisition.gov archive type."
        )
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION}",
    )
    parser.add_argument(
        "--agency",
        required=True,
        help="Archive type, for example AFARS, FAR, DFARS, or AGAR.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=script_directory / "archive",
        help=(
            "Root output directory. The script creates an agency-specific "
            "subfolder inside it. Default: ./archive beside this script."
        ),
    )
    parser.add_argument(
        "--delete-zips",
        action="store_true",
        help="Delete ZIPs after successful extraction.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Redownload and re-extract files that already exist.",
    )
    return parser.parse_args()


def main() -> int:
    print(f"Acquisition archive downloader v{SCRIPT_VERSION}")
    args = parse_args()
    agency = normalize_whitespace(args.agency).upper()
    if not agency:
        print("--agency cannot be empty.", file=sys.stderr)
        return 2

    output_root = args.output.expanduser().resolve()
    agency_directory = output_root / safe_path_component(agency, "agency")
    zip_directory = agency_directory / "_zips"
    metadata_path = agency_directory / "archive_metadata.json"
    agency_directory.mkdir(parents=True, exist_ok=True)

    session = build_session()
    try:
        records, rows_without_zips = collect_records(session, agency)
    except Exception as exc:
        print(f"\nCould not read {agency} archive pages: {exc}", file=sys.stderr)
        return 1

    print(f"\nDiscovered {len(records)} unique ZIP file(s) for {agency}.")
    print(f"Agency directory: {agency_directory}")
    print(f"Metadata file:    {metadata_path}")

    if rows_without_zips:
        print(
            f"\nNote: {len(rows_without_zips)} row(s) have no ZIP link "
            "and will be omitted:"
        )
        for number, effective_date in rows_without_zips:
            print(f"  - {number} ({effective_date})")

    successful: list[ArchiveRecord] = []
    failures: list[tuple[ArchiveRecord, str]] = []

    for index, record in enumerate(records, start=1):
        print(f"\n[{index}/{len(records)}] {record.number} — {record.effective_date}")
        destination = agency_directory / record.folder_name
        zip_path = zip_directory / record.zip_filename

        if args.overwrite:
            if destination.exists():
                shutil.rmtree(destination)
            zip_path.unlink(missing_ok=True)

        try:
            downloaded_zip = download_zip(session, record, zip_directory)
            extract_zip(downloaded_zip, record, agency_directory)
            successful.append(record)

            # Keep the JSON valid if the run is interrupted midway.
            write_metadata(successful, metadata_path)

            if args.delete_zips:
                downloaded_zip.unlink(missing_ok=True)
                print(f"[delete]   {record.zip_filename}")
        except Exception as exc:
            failures.append((record, str(exc)))
            print(f"[error]    {exc}", file=sys.stderr)

    # Creates [] if the selected archive has no downloadable ZIPs.
    write_metadata(successful, metadata_path)

    print("\nFinished.")
    print(f"Discovered ZIPs: {len(records)}")
    print(f"Extracted:       {len(successful)}")
    print(f"Failures:        {len(failures)}")
    print(f"Metadata:        {metadata_path}")

    if failures:
        print("\nFailed files:", file=sys.stderr)
        for record, error in failures:
            print(
                f"- {record.number}: {record.zip_url}\n  {error}",
                file=sys.stderr,
            )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
