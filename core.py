"""Core photo organizing logic: metadata extraction, country resolution, planning, execution.

The UI (ui.py) and the CLI (organize_photos.py) are both thin wrappers around this module.
"""

from __future__ import annotations

import datetime as dt
import functools
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

UNKNOWN_PLACE = "Unknown"

SOURCE_GPS = "GPS"
SOURCE_GPS_LOG = "GPS + log"
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
    place: str | None = None
    country_source: str = SOURCE_NONE
    destination_path: Path | None = None
    review_reason: str | None = None
    duplicate_of: Path | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None

    @property
    def needs_review(self) -> bool:
        unplaceable = self.country is None or self.capture_date is None
        return unplaceable and not self.is_duplicate


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
    skipped_files: list[Path] = field(default_factory=list)


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

    import reverse_geocoder

    coordinates = [(p.latitude, p.longitude) for p in located]
    results = reverse_geocoder.search(coordinates, mode=1)

    for plan, result in zip(located, results):
        code = (result or {}).get("cc")
        if not code:
            continue
        plan.country = canonical_country(code)
        plan.place = (result.get("name") or "").strip() or None
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
        return [], ["Travel log must have columns: Country, Place, Start Date, End Date."]
    place_col = header.index("place") if "place" in header else None

    entries: list[dict] = []
    for line_number, row in enumerate(rows[1:], start=2):
        country = row[country_col] if country_col < len(row) else None
        start = row[start_col] if start_col < len(row) else None
        end = row[end_col] if end_col < len(row) else None
        place = row[place_col] if place_col is not None and place_col < len(row) else None
        place = str(place).strip() if place is not None and str(place).strip() else None

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

        entries.append({
            "country": str(country).strip(),
            "place": place,
            "start": start_date,
            "end": end_date,
            "row": line_number,
        })

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


@functools.lru_cache(maxsize=None)
def canonical_country(name: str) -> str:
    """One spelling per country whether it came from GPS or the log.

    Otherwise a trip splits into '2025_Viet Nam' (GPS) and '2025_Vietnam' (log).
    Prefers the everyday name where pycountry has one ('South Korea' over
    'Korea, Republic of'); text pycountry doesn't recognise is kept as typed.
    """
    import pycountry

    try:
        country = pycountry.countries.lookup(name.strip())
    except LookupError:
        return name.strip()
    return getattr(country, "common_name", None) or country.name


@functools.lru_cache(maxsize=None)
def _country_key(name: str) -> str:
    """Compare countries by ISO code, so 'Viet Nam' from GPS matches 'Vietnam' in the log."""
    import pycountry

    try:
        return pycountry.countries.lookup(name.strip()).alpha_2
    except LookupError:
        return name.strip().lower()


def _label(entry: dict) -> str:
    return f"{entry['country']}, {entry['place']}" if entry["place"] else entry["country"]


def _detect_overlaps(entries: list[dict]) -> list[str]:
    warnings = []
    for i, first in enumerate(entries):
        for second in entries[i + 1:]:
            if not (first["start"] <= second["end"] and second["start"] <= first["end"]):
                continue
            rows = (
                f"Travel log rows {first['row']} ({_label(first)}) and "
                f"{second['row']} ({_label(second)}) overlap in dates"
            )
            if _country_key(first["country"]) != _country_key(second["country"]):
                warnings.append(f"{rows} - photos in the overlap will go to {NEEDS_REVIEW_DIR}.")
            elif (
                first["place"] and second["place"]
                and first["place"].lower() != second["place"].lower()
            ):
                warnings.append(
                    f"{rows} - photos without GPS in the overlap will go to "
                    f"{NEEDS_REVIEW_DIR}; photos with GPS keep their GPS city."
                )
    return warnings


def resolve_by_travel_log(plans: list[PhotoPlan], entries: list[dict]) -> None:
    """Use travel-log date ranges to fill in country and place.

    Photos without GPS take both country and place from the log. Photos with GPS
    keep their GPS country; the log only replaces their place, and only when it
    names exactly one place for that date in that same country.
    """
    if not entries:
        return

    for plan in plans:
        if plan.capture_date is None:
            continue

        day = plan.capture_date.date()
        matches = [e for e in entries if e["start"] <= day <= e["end"]]
        if not matches:
            continue

        if plan.country is not None:
            key = _country_key(plan.country)
            places = {
                e["place"].lower(): e["place"]
                for e in matches
                if e["place"] and _country_key(e["country"]) == key
            }
            if len(places) == 1:
                plan.place = next(iter(places.values()))
                plan.country_source = SOURCE_GPS_LOG
            continue

        countries = {_country_key(e["country"]): canonical_country(e["country"]) for e in matches}
        if len(countries) > 1:
            plan.review_reason = (
                f"Capture date {day} matches several travel-log countries "
                f"({', '.join(sorted(countries.values()))})."
            )
            continue

        places = {e["place"].lower(): e["place"] for e in matches if e["place"]}
        if len(places) > 1:
            plan.review_reason = (
                f"Capture date {day} matches several travel-log places "
                f"({', '.join(sorted(places.values()))})."
            )
            continue

        plan.country = next(iter(countries.values()))
        plan.place = next(iter(places.values())) if places else None
        plan.country_source = SOURCE_TRAVEL_LOG


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
    """Hands out the next free sequence number per (folder, country, place, date).

    Numbers are reserved by stem, ignoring extension, so a HEIC and an ARW taken
    on the same day get different numbers instead of both landing on _001 and
    looking like one photo stored in two formats.
    """

    def __init__(self) -> None:
        self._used: dict[Path, set[str]] = {}

    def _existing(self, folder: Path) -> set[str]:
        if folder not in self._used:
            stems = set()
            if folder.exists():
                stems = {p.stem.lower() for p in folder.iterdir() if p.is_file()}
            self._used[folder] = stems
        return self._used[folder]

    def allocate(self, folder: Path, stem: str, extension: str) -> Path:
        taken = self._existing(folder)
        counter = 1
        while True:
            candidate = f"{stem}_{counter:03d}"
            if candidate.lower() not in taken:
                taken.add(candidate.lower())
                return folder / f"{candidate}{extension}"
            counter += 1


_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _folder_name(text: str) -> str:
    """Travel-log text ends up in folder names, so strip what Windows forbids."""
    cleaned = _UNSAFE_PATH_CHARS.sub("-", text).strip().rstrip(". ")
    return cleaned or UNKNOWN_PLACE


def photo_folder(output_dir: Path, country: str, place: str | None, captured: dt.datetime) -> Path:
    """Year_Country/Place, e.g. ORGANIZED_PHOTOS/2026_China/Beijing."""
    return (
        output_dir
        / _folder_name(f"{captured.year:04d}_{country}")
        / _folder_name(place or UNKNOWN_PLACE)
    )


def photo_stem(country: str, place: str | None, captured: dt.datetime) -> str:
    """CHINA_BEIJING_26-06-01 - the allocator appends the _NNN sequence number."""
    return (
        f"{country_slug(country)}_{country_slug(place or UNKNOWN_PLACE)}"
        f"_{captured.date():%y-%m-%d}"
    )


def _resolve(plans: list[PhotoPlan], travel_log: Path | None, summary: RunSummary) -> None:
    resolve_countries_by_gps(plans)
    if travel_log and Path(travel_log).exists():
        entries, warnings = load_travel_log(Path(travel_log))
        summary.warnings.extend(warnings)
        resolve_by_travel_log(plans, entries)


def _explain_review(plan: PhotoPlan) -> None:
    if plan.review_reason is not None:
        return
    if plan.capture_date is None:
        plan.review_reason = "No capture date could be read from this file."
    elif plan.latitude is None:
        plan.review_reason = (
            f"No GPS data, and no travel-log entry covers {plan.capture_date.date()}."
        )
    else:
        plan.review_reason = "GPS coordinates could not be matched to a country."


def _tally(plan: PhotoPlan, summary: RunSummary) -> None:
    if plan.country_source in (SOURCE_GPS, SOURCE_GPS_LOG):
        summary.by_gps += 1
    else:
        summary.by_travel_log += 1


def build_plan(
    input_dir: Path,
    output_dir: Path,
    travel_log: Path | None = None,
) -> tuple[list[PhotoPlan], RunSummary]:
    """Scan the input folder and compute every photo's destination. Touches no files."""
    summary = RunSummary()
    photos, others = find_photos(input_dir)
    summary.skipped_files = others

    plans = [extract_metadata(path) for path in photos]
    summary.total = len(plans)
    _resolve(plans, travel_log, summary)

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

        if plan.needs_review:
            _explain_review(plan)
            plan.destination_path = allocator.allocate(
                review_dir, plan.source_path.stem, plan.source_path.suffix
            )
            summary.needs_review += 1
            continue

        folder = photo_folder(output_dir, plan.country, plan.place, plan.capture_date)
        stem = photo_stem(plan.country, plan.place, plan.capture_date)
        plan.destination_path = allocator.allocate(folder, stem, plan.source_path.suffix)
        _tally(plan, summary)

    return plans, summary


def refile_library(
    output_dir: Path,
    travel_log: Path | None = None,
) -> tuple[list[PhotoPlan], RunSummary]:
    """Re-derive where every photo already in the library belongs. Touches no files.

    Run this after editing the travel log or changing the layout. Photos already in
    the right folder under the right name keep their number. Photos in _NeedsReview
    that the log can now place get filed; those it still can't place stay put.
    """
    summary = RunSummary()
    photos, _ = find_photos(output_dir)
    plans = [extract_metadata(path) for path in photos]
    summary.total = len(plans)
    _resolve(plans, travel_log, summary)

    allocator = _NameAllocator()
    review_dir = output_dir / NEEDS_REVIEW_DIR

    for plan in plans:
        current = plan.source_path

        if plan.needs_review:
            _explain_review(plan)
            summary.needs_review += 1
            if current.parent != review_dir:
                plan.destination_path = allocator.allocate(review_dir, current.stem, current.suffix)
            continue

        _tally(plan, summary)
        folder = photo_folder(output_dir, plan.country, plan.place, plan.capture_date)
        stem = photo_stem(plan.country, plan.place, plan.capture_date)
        already_right = current.parent == folder and re.fullmatch(
            re.escape(stem) + r"_\d{3}", current.stem, re.IGNORECASE
        )
        if not already_right:
            plan.destination_path = allocator.allocate(folder, stem, current.suffix)

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


def remaining_files(input_dir: Path) -> list[Path]:
    """Files still sitting in the input folder after a run."""
    if not input_dir.exists():
        return []
    return sorted(p for p in input_dir.rglob("*") if p.is_file())


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

    if summary.skipped_files:
        lines.append("Not photos, left where they were:")
        lines.extend(f"  - {path}" for path in summary.skipped_files)
        lines.append("")

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
