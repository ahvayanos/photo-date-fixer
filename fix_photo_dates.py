#!/usr/bin/env python3
"""
fix_photo_dates_7.py
--------------------
Walks one level of subfolders inside a root folder and stamps each photo
inside with an EXIF date derived from the subfolder's name.

Accepted subfolder name formats (case-insensitive, Unicode-friendly):
    YYYY_MM
    YYYY_MM_anything                 e.g.  "1982_07_Καλοκαίρι_Αη_Γιάννης"
    YYYY_MM - anything               e.g.  "1982_07 - Summer in Greece"
    YYYY_MM-anything                 e.g.  "1982_07-summer"
    YYYY_MM anything                 e.g.  "1982_07 summer"

What it does:
  1. Looks at every subfolder one level under the root.
  2. For each one whose name starts with YYYY_MM, parses the year+month.
  3. Asks you (per subfolder) whether to process it. You can also quit (q)
     or accept and continue without further prompts (A = "all from here on").
  4. Re-saves each photo with the new date embedded in EXIF metadata.
     PIL re-save is used (not byte-patching) because byte-patched files can
     look valid but be rejected by strict readers like Amazon Photos.
  5. On Windows, also updates the file's creation time + modification time
     so that uploaders (Amazon Photos, OneDrive, Google Drive) that fall
     back to filesystem timestamps still see the right date.
  6. Optionally backs up files (.bak), and supports a --dry-run preview.

Fixes vs. v6:
  * [Errno 22] Invalid argument: os.utime fallback could itself raise on
    Windows for pre-1970 timestamps, leaking out as a fatal error per file.
    All filesystem-time operations are now isolated so they NEVER fail the
    EXIF write — EXIF metadata is always primary.
  * NEW: Windows file CREATION time is set via SetFileTime (ctypes), in
    addition to modification time. Amazon Photos for Windows sometimes
    falls back to file creation time when EXIF is ambiguous; before this
    fix, creation time stayed at "today" no matter what we did.
  * Per-file errors now report which step failed (open / save / move /
    timestamp) so you can see at a glance whether it's an EXIF problem
    or a filesystem one.

IMPORTANT note about Amazon Photos:
  If photos were ALREADY UPLOADED to Amazon Photos before you ran this
  script, Amazon will not re-read their metadata.  Amazon caches EXIF at
  upload time on the server; later changes to the local file don't sync
  back.  To get the new date into Amazon Photos for a previously-uploaded
  photo, you have to delete it from Amazon Photos and re-upload it.

Usage:
    python fix_photo_dates_7.py                       (asks for the root)
    python fix_photo_dates_7.py "C:\\path\\to\\root"
    python fix_photo_dates_7.py "C:\\path\\to\\root" --backup
    python fix_photo_dates_7.py "C:\\path\\to\\root" --dry-run
"""

import os
import sys
import re
import shutil
import argparse
from pathlib import Path
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Windows console: force UTF-8 so Greek (and any other non-ASCII) names
# don't crash print() with UnicodeEncodeError on classic CMD/PowerShell.
# Safe no-op on Linux/macOS and modern Windows Terminal.
# --------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import piexif
    from PIL import Image
except ImportError as e:
    print(f"\n[ERROR] Missing library: {e}")
    print("Please run:  pip install piexif Pillow")
    input("\nPress Enter to exit ...")
    sys.exit(1)

IS_WINDOWS = sys.platform == "win32"

SUPPORTED_EXT  = {".jpg", ".jpeg", ".tiff", ".tif"}

# Year + month, optionally followed by a separator (_, -, space, " - ")
# and any description.  Unicode-aware via re.UNICODE (default in Py3 for str).
FOLDER_PATTERN = re.compile(
    r"^(\d{4})_(\d{2})(?:\s*[-_ ]\s*.*)?$"
)

EXIF_DT_FORMAT = "%Y:%m:%d %H:%M:%S"

USE_COLOUR = sys.platform != "win32" or os.environ.get("WT_SESSION")


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text


BOLD   = lambda t: c(t, "1")
GREEN  = lambda t: c(t, "32")
YELLOW = lambda t: c(t, "33")
RED    = lambda t: c(t, "31")
CYAN   = lambda t: c(t, "36")
DIM    = lambda t: c(t, "2")


# --------------------------------------------------------------------------
# Windows-only: set file creation time + modification time via SetFileTime.
#
# Why this matters: os.utime() on Windows updates only mtime/atime — it
# does NOT touch the file's "creation" time (ctime).  Some photo apps,
# including the Amazon Photos Windows uploader, fall back to creation
# time when EXIF is empty or ambiguous, which means without this fix the
# file looks "created today" no matter what EXIF says.
#
# Windows FILETIME is 100-ns intervals since 1601-01-01 UTC.
# That's earlier than the Unix epoch, so even pre-1970 photo dates fit.
# --------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_WRITE        = 0x40000000
    _OPEN_EXISTING        = 3
    _FILE_FLAG_BACKUP_SEM = 0x02000000  # lets us open dirs too; harmless on files
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _SECONDS_1601_TO_1970 = 11644473600  # gap between FILETIME and Unix epoch

    _kernel32.CreateFileW.restype  = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
    ]
    _kernel32.SetFileTime.restype  = wintypes.BOOL
    _kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.CloseHandle.restype  = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _datetime_to_filetime(dt):
    """Convert a naive local datetime to a Windows FILETIME struct.
    FILETIME is a 64-bit count of 100-ns intervals since 1601-01-01 UTC."""
    # Treat dt as local (consistent with how Explorer displays times).
    # .timestamp() converts naive local to Unix epoch seconds (UTC).
    unix_seconds = dt.timestamp()
    intervals_100ns = int((unix_seconds + _SECONDS_1601_TO_1970) * 10_000_000)
    if intervals_100ns < 0:
        # Pre-1601 — should be impossible from photo dates, but guard anyway.
        intervals_100ns = 0
    ft = wintypes.FILETIME()
    ft.dwLowDateTime  = intervals_100ns & 0xFFFFFFFF
    ft.dwHighDateTime = (intervals_100ns >> 32) & 0xFFFFFFFF
    return ft


def set_filesystem_times(path, dt):
    """Set creation+modification+access time on `path` to `dt`.
    NEVER raises — filesystem time is best-effort, EXIF inside the file
    is the source of truth.  Returns a short status string for logging."""
    if IS_WINDOWS:
        try:
            ft = _datetime_to_filetime(dt)
            handle = _kernel32.CreateFileW(
                str(path), _GENERIC_WRITE, 0, None,
                _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEM, None
            )
            if handle == _INVALID_HANDLE_VALUE or handle is None:
                return "fs:open-fail"
            try:
                ok = _kernel32.SetFileTime(
                    handle, ctypes.byref(ft), ctypes.byref(ft), ctypes.byref(ft)
                )
                return "fs:ok" if ok else "fs:settime-fail"
            finally:
                _kernel32.CloseHandle(handle)
        except Exception:
            return "fs:exception"
    else:
        # POSIX: set mtime+atime; no ctime API exists portably.
        try:
            ts = dt.timestamp()
            try:
                os.utime(str(path), (ts, ts))
                return "fs:ok"
            except (OSError, OverflowError):
                # Pre-1970 dates fail on some platforms; clamp to epoch.
                try:
                    os.utime(str(path), (0, 0))
                    return "fs:clamped"
                except Exception:
                    return "fs:utime-fail"
        except Exception:
            return "fs:exception"


def prompt(msg, choices):
    try:
        return input(f"{msg} [{choices}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)


def collect_images(folder):
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT
    )


def parse_folder_date(name):
    """Return (year, month) tuple if name matches; otherwise None."""
    m = FOLDER_PATTERN.match(name)
    if not m:
        return None
    try:
        year, month = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12):
            return None
        return year, month
    except ValueError:
        return None


def get_exif_date(path):
    try:
        exif = piexif.load(str(path))
        raw = exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        if raw:
            return datetime.strptime(raw.decode(), EXIF_DT_FORMAT)
    except Exception:
        pass
    return None


def all_three_dates_already_correct(path, target_dt):
    """True if 0th.DateTime, Exif.DateTimeOriginal, Exif.DateTimeDigitized
    all already equal target_dt (to-the-second)."""
    target = target_dt.strftime(EXIF_DT_FORMAT).encode()
    try:
        exif = piexif.load(str(path))
        return (
            exif.get("0th",  {}).get(piexif.ImageIFD.DateTime)         == target and
            exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)  == target and
            exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeDigitized) == target
        )
    except Exception:
        return False


def safe_dump_exif(exif_dict):
    """piexif.dump() but tolerant of the most common failure: an embedded
    thumbnail in the '1st' IFD that is too large.  Retry without it."""
    try:
        return piexif.dump(exif_dict)
    except Exception:
        exif_dict["1st"] = {}
        exif_dict["thumbnail"] = None
        return piexif.dump(exif_dict)


def set_exif_date(path, new_dt, dry_run):
    """Re-save the image with all three standard date fields set to new_dt
    AND update filesystem creation/modification time on Windows.

    Returns a tuple: (success: bool, fs_status: str | None)
    fs_status is None when EXIF write itself failed; otherwise it reports
    how the filesystem-time update went ("fs:ok", "fs:settime-fail", etc.).

    A failure to update filesystem time does NOT count as a failure of
    set_exif_date — EXIF is what photo apps actually read.
    """
    dt_bytes = new_dt.strftime(EXIF_DT_FORMAT).encode()
    tmp_path = path.with_suffix(".tmp" + path.suffix)
    img = None
    step = "open"
    try:
        img = Image.open(str(path))
        source_format = img.format

        step = "read-exif"
        raw_exif = img.info.get("exif", b"")
        try:
            exif_dict = (
                piexif.load(raw_exif)
                if raw_exif
                else {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
            )
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

        exif_dict.setdefault("0th",  {})[piexif.ImageIFD.DateTime]         = dt_bytes
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal]  = dt_bytes
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeDigitized] = dt_bytes

        step = "dump-exif"
        exif_bytes = safe_dump_exif(exif_dict)

        if not dry_run:
            step = "save-tmp"
            try:
                if source_format == "JPEG":
                    img.save(str(tmp_path), format="JPEG", exif=exif_bytes,
                             quality=95, subsampling="keep")
                else:
                    img.save(str(tmp_path), format=source_format, exif=exif_bytes)
            finally:
                img.close()
                img = None

            step = "replace"
            shutil.move(str(tmp_path), str(path))

            step = "set-times"
            fs_status = set_filesystem_times(path, new_dt)
            return True, fs_status
        else:
            img.close()
            img = None
            return True, "dry-run"

    except Exception as ex:
        print(RED(f"    X Error during {step}: {ex}"))
        return False, None
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass
        if tmp_path.exists() and not dry_run:
            try:
                tmp_path.unlink()
            except Exception:
                pass


def process_subfolder(subfolder, new_dt, backup, dry_run):
    images = collect_images(subfolder)
    ok, skipped, errors = 0, 0, 0
    fs_failures = 0  # track filesystem-time failures separately

    if not images:
        print(DIM("    (no supported images found)"))
        return 0, 0, 0

    col_w = min(max(len(p.name) for p in images), 45)
    print(f"\n    {'File':<{col_w}}  {'Current date':<22}  {'New date':<22}  Result")
    print("    " + "-" * (col_w + 52))

    for img_path in images:
        current = get_exif_date(img_path)
        cur_str = current.strftime("%Y-%m-%d %H:%M:%S") if current else "(no EXIF date)"
        new_str = new_dt.strftime("%Y-%m-%d %H:%M:%S")

        if all_three_dates_already_correct(img_path, new_dt):
            # Even when EXIF is correct, the filesystem time may still be
            # wrong (e.g. set by a prior version of this script that didn't
            # touch ctime).  Refresh it cheaply, but don't recompress.
            if not dry_run:
                set_filesystem_times(img_path, new_dt)
            status = DIM("skip (already set)")
            name_col = img_path.name[:col_w].ljust(col_w)
            print(f"    {name_col}  {cur_str:<22}  {new_str:<22}  {status}")
            skipped += 1
            continue

        if backup and not dry_run:
            try:
                shutil.copy2(str(img_path),
                             str(img_path.with_suffix(img_path.suffix + ".bak")))
            except Exception as e:
                print(RED(f"    X Backup failed for {img_path.name}: {e}"))
                errors += 1
                continue

        success, fs_status = set_exif_date(img_path, new_dt, dry_run)
        if success and dry_run:
            status = YELLOW("dry-run")
        elif success:
            status = GREEN("OK")
            if fs_status and not fs_status.startswith("fs:ok"):
                # EXIF wrote fine, fs-time didn't — note it but don't error.
                status = GREEN("OK") + DIM(f" ({fs_status})")
                fs_failures += 1
        else:
            status = RED("ERROR")

        name_col = img_path.name[:col_w].ljust(col_w)
        print(f"    {name_col}  {cur_str:<22}  {new_str:<22}  {status}")

        if success:
            ok += 1
        else:
            errors += 1

    if fs_failures:
        print(DIM(f"    note: {fs_failures} file(s) had filesystem-time "
                  f"updates skipped — EXIF inside the file is still correct"))

    return ok, skipped, errors


def find_target_subfolders(root):
    """Return list of (subfolder Path, year, month) for every immediate
    subfolder of root whose name parses to a YYYY_MM."""
    matched, unmatched = [], []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        ym = parse_folder_date(p.name)
        if ym:
            matched.append((p, ym[0], ym[1]))
        else:
            unmatched.append(p)
    return matched, unmatched


def run(root, backup, dry_run):
    print()
    print(BOLD("=" * 62))
    print(BOLD("  Photo Date Fixer"))
    print(BOLD("=" * 62))
    if dry_run:
        print(YELLOW("  DRY-RUN mode - no files will be modified\n"))
    if backup:
        print(CYAN("  BACKUP mode - .bak copies saved before changes\n"))

    matched, unmatched = find_target_subfolders(root)

    if not matched and len(unmatched) == 1:
        candidate = unmatched[0]
        sub_matched, _ = find_target_subfolders(candidate)
        if sub_matched:
            print(YELLOW(f"  No YYYY_MM folders directly in: {root}"))
            print(YELLOW(f"  But found {len(sub_matched)} inside: {candidate.name}"))
            ans = prompt(f"  Descend into '{candidate.name}'?", "y/n")
            if ans in ("y", "yes"):
                root = candidate
                matched, unmatched = sub_matched, []
            else:
                print("Cancelled.")
                input("\nPress Enter to exit ...")
                sys.exit(0)

    if not matched:
        print(RED("No subfolders matching YYYY_MM(_/-/space)description in:"))
        print(f"  {root}")
        print("\nExpected examples:  1982_11   1982_07_Kalokeri   1982_07 - Summer")
        input("\nPress Enter to exit ...")
        sys.exit(1)

    print(f"  Root folder : {root}")
    print(f"  Subfolders  : {len(matched)} matched, {len(unmatched)} skipped")
    if unmatched:
        names = ", ".join(p.name for p in unmatched[:8])
        more = f" (+{len(unmatched) - 8} more)" if len(unmatched) > 8 else ""
        print(DIM(f"  Skipped     : {names}{more}"))
    print()

    total_ok = total_skip = total_err = 0
    auto_yes = False

    for subfolder, year, month in matched:
        new_dt = datetime(year, month, 1, 12, 0, 0)
        images = collect_images(subfolder)

        print(BOLD("=" * 62))
        print(BOLD(f"  {subfolder.name}"))
        print(f"     Date to apply : {CYAN(new_dt.strftime('%Y-%m-%d'))}  (day=01, time=12:00)")
        print(f"     Images found  : {len(images)}")

        if len(images) == 0:
            print(DIM("     No supported images - skipping."))
            print()
            continue

        if auto_yes:
            ans = "y"
            print(DIM("  (auto-yes mode)"))
        else:
            ans = prompt(
                "\n  Process this folder?",
                "y=yes / n=skip / A=yes-to-all / q=quit"
            )

        if ans in ("q", "quit"):
            print("\nQuit by user.")
            break
        if ans == "a":
            auto_yes = True
            ans = "y"
        if ans not in ("y", "yes"):
            print(YELLOW("  -> Skipped.\n"))
            total_skip += len(images)
            continue

        ok, sk, err = process_subfolder(subfolder, new_dt, backup, dry_run)
        total_ok   += ok
        total_skip += sk
        total_err  += err
        print()

    print(BOLD("=" * 62))
    print(BOLD("  Summary"))
    print(BOLD("=" * 62))
    print(f"  Updated  : {total_ok}")
    print(f"  Skipped  : {total_skip}")
    print(f"  Errors   : {total_err}")
    if dry_run:
        print(YELLOW("\n  No files were changed (dry-run mode)."))
    print()

    print(DIM("  Reminder: if these photos are already in Amazon Photos,"))
    print(DIM("  you must DELETE and RE-UPLOAD them to pick up the new dates."))
    print(DIM("  Amazon caches EXIF at upload time and won't re-read it.\n"))

    input("Press Enter to exit ...")


def main():
    parser = argparse.ArgumentParser(
        description="Fix EXIF dates on photos using subfolder names.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("root", nargs="?", default=None,
                        help="Root folder containing YYYY_MM subfolders")
    parser.add_argument("--backup", action="store_true",
                        help="Save a .bak copy of each file before modifying")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing anything")
    args = parser.parse_args()

    if args.root is None:
        print("\n  Photo Date Fixer")
        print("  -----------------")
        print("  Enter the path to your root photos folder.")
        print("  (It should contain subfolders like: 1982_07 or 1982_07_Kalokeri)\n")
        try:
            raw = input("  Folder path: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)
        root = Path(raw)
    else:
        root = Path(args.root)

    if not root.is_dir():
        print(f"\n[ERROR] Not a valid folder: {root}")
        input("Press Enter to exit ...")
        sys.exit(1)

    run(root, backup=args.backup, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
