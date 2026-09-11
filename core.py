"""Core photo organizing logic: metadata extraction, country resolution, planning, execution.

The UI (ui.py) and the CLI (organize_photos.py) are both thin wrappers around this module.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import exifread

PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef",
}

NEEDS_REVIEW_DIR = "_NeedsReview"

SOURCE_GPS = "GPS"
SOURCE_TRAVEL_LOG = "Travel log"
SOURCE_NONE = "-"


@dataclass
class PhotoPlan:
    """One photo's planned destination. `country` is None when it needs review."""

    source_path: Path
    capture_date: dt.datetime | None = None
    date_from_exif: bool = False
    latitude: float | None = None
    longitude: float | None = None
    country: str | None = None
    country_source: str = SOURCE_NONE
    destination_path: Path | None = None
    review_reason: str | None = None
    duplicate_of: Path | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None

    @property
    def needs_review(self) -> bool:
        return self.country is None and not self.is_duplicate


@dataclass
class RunSummary:
    total: int = 0
    by_gps: int = 0
    by_travel_log: int = 0
    needs_review: int = 0
    duplicates: int = 0
    moved: int = 0
    failed: list[tuple[Path, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------------------

def _ratio_to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(value.num) / float(value.den)


def _dms_to_degrees(values, ref: str | None) -> float | None:
    """Convert EXIF degrees/minutes/seconds to a signed decimal degree."""
    try:
        degrees = _ratio_to_float(values[0])
        minutes = _ratio_to_float(values[1])
        seconds = _ratio_to_float(values[2])
    except (IndexError, AttributeError, ZeroDivisionError):
        return None

    result = degrees + minutes / 60.0 + seconds / 3600.0
    if ref and ref.upper().startswith(("S", "W")):
        result = -result
    return result


def _parse_exif_datetime(raw: str) -> dt.datetime | None:
    raw = raw.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _read_with_exifread(path: Path):
    """Return (datetime|None, lat|None, lon|None) using exifread (handles JPEG/TIFF/RAW)."""
    with open(path, "rb") as handle:
        tags = exifread.process_file(handle, details=False)

    if not tags:
        return None, None, None

    captured = None
    for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
        if key in tags:
            captured = _parse_exif_datetime(str(tags[key]))
            if captured:
                break

    latitude = longitude = None
    if "GPS GPSLatitude" in tags and "GPS GPSLongitude" in tags:
        latitude = _dms_to_degrees(
            tags["GPS GPSLatitude"].values, str(tags.get("GPS GPSLatitudeRef", ""))
        )
        longitude = _dms_to_degrees(
            tags["GPS GPSLongitude"].values, str(tags.get("GPS GPSLongitudeRef", ""))
        )

    return captured, latitude, longitude


def _read_with_pillow(path: Path):
    """Fallback for HEIC/HEIF, which exifread does not always parse."""
    from PIL import Image, ExifTags

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    with Image.open(path) as image:
        exif = image.getexif()
        if not exif:
            return None, None, None

        captured = None
        base = exif.get_ifd(ExifTags.IFD.Exif) or {}
        for tag in (ExifTags.Base.DateTimeOriginal, ExifTags.Base.DateTimeDigitized):
            raw = base.get(tag)
            if raw:
                captured = _parse_exif_datetime(str(raw))
                if captured:
                    break
        if captured is None and exif.get(ExifTags.Base.DateTime):
            captured = _parse_exif_datetime(str(exif[ExifTags.Base.DateTime]))

        latitude = longitude = None
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo) or {}
        if gps.get(2) and gps.get(4):
            latitude = _dms_to_degrees(gps[2], str(gps.get(1, "")))
            longitude = _dms_to_degrees(gps[4], str(gps.get(3, "")))

        return captured, latitude, longitude


def extract_metadata(path: Path) -> PhotoPlan:
    """Read capture date and GPS for one photo. Never raises on a bad file."""
    plan = PhotoPlan(source_path=path)

    for reader in (_read_with_exifread, _read_with_pillow):
        try:
            captured, latitude, longitude = reader(path)
        except Exception:
            continue

        if plan.capture_date is None and captured is not None:
            plan.capture_date = captured
            plan.date_from_exif = True
        if plan.latitude is None and latitude is not None and longitude is not None:
            plan.latitude, plan.longitude = latitude, longitude

        if plan.capture_date is not None and plan.latitude is not None:
            break

    if plan.capture_date is None:
        try:
            plan.capture_date = dt.datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            plan.capture_date = None

    return plan


# --------------------------------------------------------------------------------------
# Country resolution
# --------------------------------------------------------------------------------------

def resolve_countries_by_gps(plans: list[PhotoPlan]) -> None:
    """Offline reverse-geocode every plan that has coordinates, in one batch."""
    located = [p for p in plans if p.latitude is not None and p.longitude is not None]
    if not located:
        return

    import pycountry
    import reverse_geocoder

    coordinates = [(p.latitude, p.longitude) for p in located]
    results = reverse_geocoder.search(coordinates, mode=1)

    for plan, result in zip(located, results):
        code = (result or {}).get("cc")
        if not code:
            continue
        country = pycountry.countries.get(alpha_2=code)
        plan.country = country.name if country else code
        plan.country_source = SOURCE_GPS


def load_travel_log(path: Path) -> tuple[list[dict], list[str]]:
    """Read the travel-log workbook. Returns (entries, warnings)."""
    import openpyxl

    warnings: list[str] = []
    workbook = openpyxl.load_workbook(path, data_only=True)
    sheet = workbook.active

    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return [], ["Travel log is empty."]

    header = [str(cell).strip().lower() if cell else "" for cell in rows[0]]
    try:
        country_col = header.index("country")
        start_col = header.index("start date")
        end_col = header.index("end date")
    except ValueError:
        return [], ["Travel log must have columns: Country, Start Date, End Date."]

    entries: list[dict] = []
    for line_number, row in enumerate(rows[1:], start=2):
        country = row[country_col] if country_col < len(row) else None
        start = row[start_col] if start_col < len(row) else None
        end = row[end_col] if end_col < len(row) else None

        if not country or start is None or end is None:
            continue

        start_date = _coerce_date(start)
        end_date = _coerce_date(end)
        if start_date is None or end_date is None:
            warnings.append(f"Travel log row {line_number}: unreadable date, row skipped.")
            continue
        if start_date > end_date:
            warnings.append(
                f"Travel log row {line_number} ({country}): start date is after end date, row skipped."
            )
            continue

        entries.append(
            {"country": str(country).strip(), "start": start_date, "end": end_date, "row": line_number}
        )

    warnings.extend(_detect_overlaps(entries))
    return entries, warnings


def _coerce_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _detect_overlaps(entries: list[dict]) -> list[str]:
    warnings = []
    for i, first in enumerate(entries):
        for second in entries[i + 1:]:
            if first["start"] <= second["end"] and second["start"] <= first["end"]:
                if first["country"].lower() != second["country"].lower():
                    warnings.append(
                        f"Travel log rows {first['row']} ({first['country']}) and "
                        f"{second['row']} ({second['country']}) overlap in dates - "
                        f"photos in the overlap will go to {NEEDS_REVIEW_DIR}."
                    )
    return warnings


def resolve_countries_by_travel_log(plans: list[PhotoPlan], entries: list[dict]) -> None:
    """Fill in countries for GPS-less photos using the travel log date ranges."""
    if not entries:
        return

    for plan in plans:
        if plan.country is not None or plan.capture_date is None:
            continue

        captured = plan.capture_date.date()
        matches = {
            entry["country"]
            for entry in entries
            if entry["start"] <= captured <= entry["end"]
        }

        if len(matches) == 1:
            plan.country = matches.pop()
            plan.country_source = SOURCE_TRAVEL_LOG
        elif len(matches) > 1:
            plan.review_reason = (
                f"Capture date {captured} matches several travel-log countries "
                f"({', '.join(sorted(matches))})."
            )


# --------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------

def country_slug(country: str) -> str:
    """'United States' -> 'UNITED_STATES'; strips accents so filenames stay ASCII-safe."""
    normalized = unicodedata.normalize("NFKD", country)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_")
    return slug.upper() or "UNKNOWN"


def find_photos(input_dir: Path) -> tuple[list[Path], list[Path]]:
    """Recursively split the input folder into (photos, other files)."""
    photos, others = [], []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in PHOTO_EXTENSIONS:
            photos.append(path)
        else:
            others.append(path)
    return photos, others


def file_digest(path: Path) -> str:
    """SHA-256 of the file's bytes. Content-based, so renaming doesn't affect it."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def index_library_by_size(output_dir: Path) -> dict[int, list[Path]]:
    """Map byte-size -> photos already in the library.

    Size is the cheap pre-filter: two files of different sizes can never be
    identical, so only same-size candidates are ever hashed.
    """
    index: dict[int, list[Path]] = {}
    if not output_dir.exists():
        return index
    for path in output_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in PHOTO_EXTENSIONS:
            try:
                index.setdefault(path.stat().st_size, []).append(path)
            except OSError:
                continue
    return index


def _cached_digest(path: Path, cache: dict[Path, str]) -> str:
    if path not in cache:
        cache[path] = file_digest(path)
    return cache[path]


def find_duplicate(
    path: Path,
    library: dict[int, list[Path]],
    batch: dict[int, list[Path]],
    cache: dict[Path, str],
) -> Path | None:
    """Return the existing photo `path` duplicates, or None."""
    try:
        size = path.stat().st_size
    except OSError:
        return None

    rivals = list(library.get(size, ())) + list(batch.get(size, ()))
    if not rivals:
        return None

    try:
        mine = _cached_digest(path, cache)
    except OSError:
        return None

    for other in rivals:
        try:
            if _cached_digest(other, cache) == mine:
                return other
        except OSError:
            continue
    return None


class _NameAllocator:
    """Hands out the next free sequence number per (folder, country, date)."""

    def __init__(self) -> None:
        self._used: dict[Path, set[str]] = {}

    def _existing(self, folder: Path) -> set[str]:
        if folder not in self._used:
            names = set()
            if folder.exists():
                names = {p.name.lower() for p in folder.iterdir() if p.is_file()}
            self._used[folder] = names
        return self._used[folder]

    def allocate(self, folder: Path, stem: str, extension: str) -> Path:
        taken = self._existing(folder)
        counter = 1
        while True:
            candidate = f"{stem}_{counter:03d}{extension}"
            if candidate.lower() not in taken:
                taken.add(candidate.lower())
                return folder / candidate
            counter += 1


def photo_folder(output_dir: Path, country: str, captured: dt.datetime) -> Path:
    """Country/Year/Month, e.g. ORGANIZED_PHOTOS/China/2026/06-June."""
    month = f"{captured.month:02d}-{calendar.month_name[captured.month]}"
    return output_dir / country / f"{captured.year:04d}" / month


def build_plan(
    input_dir: Path,
    output_dir: Path,
    travel_log: Path | None = None,
) -> tuple[list[PhotoPlan], RunSummary]:
    """Scan the input folder and compute every photo's destination. Touches no files."""
    summary = RunSummary()
    photos, others = find_photos(input_dir)

    if others:
        summary.warnings.append(
            f"{len(others)} non-photo file(s) found in the input folder - these are left untouched."
        )

    plans = [extract_metadata(path) for path in photos]
    summary.total = len(plans)

    resolve_countries_by_gps(plans)

    if travel_log and Path(travel_log).exists():
        entries, warnings = load_travel_log(Path(travel_log))
        summary.warnings.extend(warnings)
        resolve_countries_by_travel_log(plans, entries)

    allocator = _NameAllocator()
    review_dir = output_dir / NEEDS_REVIEW_DIR
    library = index_library_by_size(output_dir)
    digest_cache: dict[Path, str] = {}
    batch: dict[int, list[Path]] = {}

    for plan in plans:
        original = find_duplicate(plan.source_path, library, batch, digest_cache)
        if original is not None:
            plan.duplicate_of = original
            summary.duplicates += 1
            continue

        try:
            batch.setdefault(plan.source_path.stat().st_size, []).append(plan.source_path)
        except OSError:
            pass

        if plan.country is None or plan.capture_date is None:
            if plan.review_reason is None:
                if plan.capture_date is None:
                    plan.review_reason = "No capture date could be read from this file."
                elif plan.latitude is None:
                    plan.review_reason = (
                        "No GPS data, and no travel-log entry covers "
                        f"{plan.capture_date.date()}."
                    )
                else:
                    plan.review_reason = "GPS coordinates could not be matched to a country."
            plan.destination_path = allocator.allocate(
                review_dir, plan.source_path.stem, plan.source_path.suffix
            )
            summary.needs_review += 1
            continue

        folder = photo_folder(output_dir, plan.country, plan.capture_date)
        stem = f"{country_slug(plan.country)}_{plan.capture_date.date():%Y-%m-%d}"
        plan.destination_path = allocator.allocate(folder, stem, plan.source_path.suffix)

        if plan.country_source == SOURCE_GPS:
            summary.by_gps += 1
        else:
            summary.by_travel_log += 1

    return plans, summary


# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------

def execute_plan(
    plans: list[PhotoPlan],
    summary: RunSummary,
    move: bool = True,
    progress=None,
) -> RunSummary:
    """Move (or copy) each photo to its planned destination."""
    for index, plan in enumerate(plans, start=1):
        if plan.destination_path is None:
            continue
        try:
            plan.destination_path.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(plan.source_path), str(plan.destination_path))
            else:
                shutil.copy2(str(plan.source_path), str(plan.destination_path))
            summary.moved += 1
        except Exception as error:
            summary.failed.append((plan.source_path, str(error)))

        if progress:
            progress(index, len(plans))

    return summary


def remove_empty_subfolders(root: Path) -> None:
    """Clean up directories left behind in the input folder after moving files out."""
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                path.rmdir()
            except OSError:
                pass


def write_review_log(output_dir: Path, plans: list[PhotoPlan], summary: RunSummary) -> Path:
    """Write a timestamped log of everything that needs manual attention."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"organizer_log_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"

    lines = [
        f"Photo organizer run - {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        f"Total photos processed : {summary.total}",
        f"Matched by GPS         : {summary.by_gps}",
        f"Matched by travel log  : {summary.by_travel_log}",
        f"Needs review           : {summary.needs_review}",
        f"Duplicates skipped     : {summary.duplicates}",
        f"Files moved/copied     : {summary.moved}",
        "",
    ]

    duplicates = [p for p in plans if p.is_duplicate]
    if duplicates:
        lines.append("Duplicates left in the input folder (already in the library):")
        for plan in duplicates:
            lines.append(f"  - {plan.source_path.name}: same content as {plan.duplicate_of}")
        lines.append("")

    if summary.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in summary.warnings)
        lines.append("")

    review = [p for p in plans if p.needs_review]
    if review:
        lines.append("Photos needing manual review:")
        for plan in review:
            lines.append(f"  - {plan.source_path.name}: {plan.review_reason}")
        lines.append("")

    if summary.failed:
        lines.append("Files that could not be processed:")
        lines.extend(f"  - {path.name}: {reason}" for path, reason in summary.failed)
        lines.append("")

    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path
