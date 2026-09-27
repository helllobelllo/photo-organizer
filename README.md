# Photo Organizer

Sorts travel photos by country and place, and renames them from their own metadata.

A photo taken in Paris on 5 June 2024 becomes:

```
ORGANIZED_PHOTOS/2024_France/Paris/FRANCE_PARIS_24-06-05_001.jpg
```

Folders are always **Year_Country / Place**.

Country and place come from the GPS coordinates stored in the photo's EXIF data,
looked up **offline** — your location history never leaves your machine. Photos
without GPS (most dedicated cameras) fall back to an Excel travel log you keep
yourself.

## Install

Requires Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

## Use the app

```bash
python ui.py
```

The window has two buttons, meant to be used in order:

1. **Scan input folder** — reads every photo and shows you exactly where each one
   would go. Nothing is moved. Photos that can't be placed appear in orange.
2. **Organize & move photos** — moves them for real, after a confirmation prompt.

A third button, **Re-file library**, re-checks photos that are *already* organized
— see below.

By default it reads from `Desktop\INPUT_PHOTO_ORGANIZOR` and writes to
`Desktop\ORGANIZED_PHOTOS`. Both paths are editable in the window.

Photos are **moved**, not copied, so the input folder ends up empty — except for
duplicates, which are deliberately left behind (see below). Files that aren't
photos are left where they are.

## Dropping in folders

You can put loose photos in the input folder, or whole folders, or folders of
folders — a card dump, a shared album, an old backup tree. Everything is searched
recursively to any depth, and the original folder names have no effect on where
photos end up: only their metadata decides that.

After a run, folders that have been emptied are removed for you. Anything that
isn't a photo stays exactly where it is, and the app lists what's left and where,
so nothing quietly disappears into a subfolder you forget about.

Nested imports commonly contain the same photo more than once (a backup folder
alongside the originals). Those are caught by content — see Duplicate detection
below.

## Where "place" comes from

**Photos with GPS** get the nearest town or city from the offline GeoNames
database: the Forbidden City gives `Beijing`, the Colosseum gives `Rome`.

Nearest-town lookup has one quirk: near big cities it often names a suburb or
district. The Eiffel Tower resolves to `Vanves`, and the Bund to `Hongkou`. Left
alone, one Paris trip would scatter across several folders. So the **travel log
can override it**. If your log has a row covering that date, in the same country,
with a Place filled in, that place wins. Log `France, Paris` for the week and every
photo from it lands in `Paris`, whatever suburb GPS picked. The app shows these
as matched by **GPS + log**.

**Photos without GPS** take both country and place from the travel log.

A row with no Place files its photos under `Unknown`. Fill the Place in later and
use **Re-file library** to move them.

Countries get one spelling whichever source they came from: log `Vietnam` and GPS
`Viet Nam` both become `Vietnam`. Otherwise one trip would split across two folders.

## The travel log

Open `travel_log_template.xlsx`, fill in one row per stay, and save it somewhere
you'll keep it. The app picks up `travel_log.xlsx` from your Desktop automatically.

| Country | Place | Start Date | End Date   |
| ------- | ----- | ---------- | ---------- |
| France  | Paris | 2024-06-01 | 2024-06-09 |
| France  | Nice  | 2024-06-10 | 2024-06-14 |
| Italy   | Rome  | 2024-06-15 | 2024-06-22 |

Dates must be `YYYY-MM-DD`, and Place is optional. Logs from before the Place
column existed still work.

**Travel days:** the log only has dates, not times. So on a day covered by two
places, a camera photo can't be placed, and it goes to `_NeedsReview` rather than
being guessed. Photos with GPS are unaffected; they keep their GPS city. To avoid
this, end one stay the day before the next begins, as in the example above. The
app warns you about any overlapping rows.

Regenerate a fresh blank template any time:

```bash
python make_travel_log_template.py
```

## Duplicate detection

Before moving anything, each photo is compared against everything already in the
output folder. A photo that's already there is **left in the input folder and
flagged** (shown in grey in the app) rather than filed a second time.

The comparison is on **file content**, not filename — a SHA-256 hash of the bytes.
Renaming is exactly what this tool does to your photos, so names are useless for
identity; a photo already filed as `CHINA_BEIJING_26-06-01_001.ARW` is still
recognised when you re-import it as `DSC00605.ARW`.

Duplicates within a single batch are caught too: if the same photo appears twice
in the input folder, the first is filed and the second is flagged.

Hashing every photo in a large library on every scan would be far too slow, so
file size is used as a pre-filter — two files of different sizes cannot be
identical, so only same-size candidates are ever hashed. Size alone is never
treated as proof; two different photos that happen to share a byte count are
correctly kept apart.

Because duplicates stay put, the input folder won't be empty after a run that
found any. That's deliberate — deleting photos is left to you.

**Limitation:** this detects byte-identical files. A photo that has been
re-exported, resized, or had its metadata rewritten is a different file and will
be filed as a new photo.

## What lands in `_NeedsReview`

A photo goes to `ORGANIZED_PHOTOS/_NeedsReview/` in four cases:

- it has no GPS and no travel-log row covers its date
- no capture date can be read at all
- its date falls in travel-log rows for different countries
- it has no GPS and its date falls in rows for different places

Every run writes an `organizer_log_*.txt` into the output folder listing each file
and the reason.

## Re-file library

Organizing only ever looks at the input folder. **Re-file library** instead
re-checks every photo already in the output folder against the current rules and
travel log, and moves the ones that no longer belong where they are. Use it after:

- filling in a Place that was left blank, so photos move out of `Unknown`
- adding a trip that explains photos sitting in `_NeedsReview`
- overriding a GPS suburb, e.g. logging `Paris` to gather up `Vanves`

It previews first, like a scan. Photos already in the right place keep their names
and numbers, so running it when nothing has changed moves nothing. Nothing leaves
the library and nothing is deleted.

## Folders and file naming

```
ORGANIZED_PHOTOS/
  2025_Vietnam/
    Old Quarter/  VIETNAM_OLD_QUARTER_25-01-10_001.jpg
  2026_China/
    Beijing/      CHINA_BEIJING_26-06-01_001.ARW
                  CHINA_BEIJING_26-06-01_002.HEIC
    Shanghai/     CHINA_SHANGHAI_26-06-10_001.ARW
    Unknown/      CHINA_UNKNOWN_26-06-20_001.ARW
  _NeedsReview/
```

Files are named `COUNTRY_PLACE_YY-MM-DD_NNN.ext`, where `NNN` starts at `001` and
counts up for each photo sharing the same country, place and date. Names are made
filename-safe: uppercased, accents removed, spaces and punctuation turned into `_`.

The year is two digits in the **filename** but four in the **folder**. The full year
is never lost, which matters because `25` alone can't tell 1925 from 2025.

Existing folders are reused, never replaced. Re-running is safe: numbering
continues past whatever is already in the destination, so nothing is overwritten.

## Command line

The same logic without the window. It previews by default and only touches files
when you pass `--run`:

```bash
python organize_photos.py "C:\Users\You\Desktop\INPUT_PHOTO_ORGANIZOR" "C:\Users\You\Desktop\ORGANIZED_PHOTOS" --travel-log travel_log.xlsx
```

```bash
python organize_photos.py "C:\Users\You\Desktop\INPUT_PHOTO_ORGANIZOR" "C:\Users\You\Desktop\ORGANIZED_PHOTOS" --travel-log travel_log.xlsx --run
```

Other flags: `--copy` (leave originals in place). Re-filing the library is
available in the app only.

## Supported files

| Format | Extensions | Notes |
| ------ | ---------- | ----- |
| JPEG | `.jpg` `.jpeg` | Date + GPS verified |
| HEIC / HEIF | `.heic` `.heif` | iPhone default; date + GPS verified |
| PNG | `.png` | Date + GPS verified when present |
| TIFF | `.tif` `.tiff` | Date + GPS verified |
| RAW | `.cr2` `.cr3` `.nef` `.arw` `.dng` `.orf` `.rw2` `.raf` `.pef` | Date verified on Sony `.ARW` |

All formats go through the same pipeline — folders, naming, travel-log fallback
and duplicate detection behave identically regardless of type, and a single
import can mix them freely.

RAW files are read with `exifread`, which parses their headers directly without
decoding the image. HEIC needs `pillow-heif`, which is in `requirements.txt`.
Mixed imports are numbered so each photo gets its own sequence number: an iPhone
HEIC and a camera RAW shot the same day become `_001` and `_002`, not two files
both called `_001`.

Most RAW files carry no GPS, so they rely on the travel log. Phone photos
normally carry GPS and sort themselves.

To add a format, add its extension to `PHOTO_EXTENSIONS` in `core.py` — anything
`exifread` or Pillow can open will work.

A corrupt or unreadable file is logged and skipped — it never stops the run.

## Project layout

| File                          | Purpose                                             |
| ----------------------------- | --------------------------------------------------- |
| `core.py`                     | Metadata, country/place lookup, naming, moving       |
| `ui.py`                       | The desktop window                                   |
| `organize_photos.py`          | Command-line interface                               |
| `make_travel_log_template.py` | Regenerates the blank travel log                     |
| `travel_log_template.xlsx`    | Starter travel log to fill in                        |
