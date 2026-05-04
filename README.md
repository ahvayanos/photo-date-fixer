# Small Vibe Coding Projects

A collection of small utility scripts for personal use.

## Photo Date Fixer

`fix_photo_dates_7.py` — stamps EXIF dates onto photos based on the folder they live in. Useful for old scanned albums with no EXIF date, where photo apps would otherwise pile everything under "no date" or "today".

### How it works

Organise photos into one folder per album/event, named with the year and month at the start:

```
Pictures/
  1960_01/
  1966_01_Album_1966_1977/
  1982_07_Summer_trip/
  1985_03 - Spring Trip/
```

Run the script and it walks each subfolder, parses the year and month from the folder name, and writes that date into the EXIF metadata of every JPEG/TIFF inside. It asks for confirmation before touching each folder.

Accepted folder name formats:

- `YYYY_MM`
- `YYYY_MM_anything`
- `YYYY_MM-anything`
- `YYYY_MM anything`
- `YYYY_MM - anything`

Non-ASCII characters (Greek, accents, etc.) in folder or file names are fine.

### Requirements

- Python 3.8 or newer
- `pip install piexif Pillow`

### Usage

```bash
# Interactive — asks for the root folder
python fix_photo_dates_7.py

# With a path argument
python fix_photo_dates_7.py "C:\path\to\Pictures"

# Preview without writing anything
python fix_photo_dates_7.py "C:\path\to\Pictures" --dry-run

# Save .bak copies before modifying
python fix_photo_dates_7.py "C:\path\to\Pictures" --backup
```

For each subfolder you'll be prompted: `y` = process, `n` = skip, `A` = yes to all remaining, `q` = quit.

### What it writes

Three EXIF date fields, each set to the first of the month at noon (e.g. `1982:07:01 12:00:00`):

- `0th.DateTime`
- `Exif.DateTimeOriginal`
- `Exif.DateTimeDigitized`

It also updates the file's modification time — and on Windows, the file's creation time — in case a photo app falls back to filesystem timestamps.

Files that already have all three EXIF date fields correctly set are skipped automatically, so re-running the script is safe and cheap.

### Heads-up: Amazon Photos

If photos were already uploaded to Amazon Photos *before* you fixed the EXIF, the cloud copies won't update. Amazon reads EXIF at upload time and caches it server-side. To pick up corrected dates you have to delete and re-upload the affected photos.

## License

Personal-use scripts, provided as-is. Feel free to adapt.
