"""
photos-fix-dates -- fix wrong photo dates in Apple Photos after an
osxphotos import.

Ports the already-mostly-Python `fix_media_dates.py`, found necessary
while running a real Apple Photos import in production.

Background: `osxphotos import --exiftool` only sets title/description/
keywords/location -- it never sets the photo's date. Date-setting is left
entirely to Apple Photos' own native AppleScript import behavior, which
reads the file's embedded date if it can, otherwise falls back to
file-modification-time. Photos' native EXIF reading doesn't reliably
cover every format -- GIF and PNG specifically get silently mis-dated to
file-modification-time even though `exiftool -DateTimeOriginal` reads a
valid embedded date from the same file. This is a Photos-side gap, not
something `--exiftool` or any osxphotos import flag controls.

This module finds photos in a given album whose Photos date falls in a
suspicious recent window (e.g. the day(s) the import ran) and corrects
them to their real `exiftool`-read date via `photoscript` (osxphotos'
own AppleScript library) -- no re-import needed, this edits the date on
the already-imported asset directly.

Tag fallback: tries DateTimeOriginal, then CreateDate, then
MediaCreateDate, using the first one whose value is NOT itself inside the
suspect window. This matters because the GIF/PNG bug above (Photos not
reading DateTimeOriginal at all) is a different failure mode from a file
whose DateTimeOriginal/CreateDate is itself corrupted at the source --
seen once on a real .mp4 where both tags held a bogus future placeholder
while MediaCreateDate (a QuickTime-container-specific tag) held the real
date. Without this fallback, a naive fix would reapply the same wrong
date. If all three tags are still in the suspect window, the file is
reported skipped rather than writing a value that can't be trusted.

Testability: `query_fn` and `tag_reader_fn` replace bare `subprocess.run`
calls, and the actual `photoscript.Photo(uuid).date = ...` write is
behind an injectable `date_setter_fn` that only imports `photoscript`
when actually invoked for real -- lets the test suite exercise every
path with zero real dependencies.
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import _osxphotos
from ._preflight import PreflightResult
from ._report import RunSummary

DATE_TAGS = ("DateTimeOriginal", "CreateDate", "MediaCreateDate")
DEFAULT_EXIFTOOL_BIN = "exiftool"

QueryFn = Callable[[str, str, str], List[dict]]  # (osxphotos_bin, album, suspect_after) -> items
TagReaderFn = Callable[[Path, str], Optional[str]]  # (path, tag) -> raw exiftool value or None
DateSetterFn = Callable[[str, datetime], None]  # (uuid, new_date) -> None
WhichFn = _osxphotos.WhichFn
PlatformFn = _osxphotos.PlatformFn


def _default_query_fn(osxphotos_bin: str, album: str, suspect_after: str) -> List[dict]:
    result = subprocess.run(  # nosec B603 -- list-form argv, no shell=True
        [osxphotos_bin, "query", "--album", album, "--from-date", suspect_after, "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osxphotos query failed (exit {result.returncode}): {result.stderr}")
    return json.loads(result.stdout)


def _default_tag_reader_fn(path: Path, tag: str, exiftool_bin: str = DEFAULT_EXIFTOOL_BIN) -> Optional[str]:
    result = subprocess.run(  # nosec B603 -- list-form argv, no shell=True
        [exiftool_bin, "-s3", f"-{tag}", str(path)],
        capture_output=True, text=True,
    )
    value = result.stdout.strip()
    return value or None


def _default_date_setter_fn(uuid: str, new_date: datetime) -> None:
    import photoscript  # deferred: only needed for the actual write, and only present in the osxphotos venv

    photoscript.Photo(uuid).date = new_date


def _parse_exiftool_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def read_exif_date(
    path: Path, suspect_after: datetime, tag_reader_fn: TagReaderFn
) -> Optional[Tuple[datetime, str]]:
    """Tries each tag in DATE_TAGS in order, returning the first whose value
    is NOT itself >= suspect_after (i.e. not itself suspicious), along with
    which tag it came from. Falls back to the last readable tag (even if
    still suspicious) only if every tag was either unreadable or
    suspicious -- returns None only if no tag was readable at all."""
    last_reading: Optional[Tuple[datetime, str]] = None
    for tag in DATE_TAGS:
        raw = tag_reader_fn(path, tag)
        value = _parse_exiftool_date(raw)
        if value is None:
            continue
        last_reading = (value, tag)
        if value < suspect_after:
            return value, tag
    return last_reading


def preflight(
    source_dir: Path,
    osxphotos_bin: str,
    exiftool_bin: str,
    which_fn: WhichFn,
    platform_fn: PlatformFn,
) -> PreflightResult:
    if not source_dir.is_dir():
        return PreflightResult.failed(f"source_dir does not exist or is not a directory: {source_dir}")

    reasons = []
    platform_result = _osxphotos.preflight_platform_and_binary(osxphotos_bin, which_fn, platform_fn)
    if not platform_result.ok:
        reasons.extend(platform_result.reasons)
    if which_fn(exiftool_bin) is None:
        reasons.append(
            f"exiftool binary not found on PATH: {exiftool_bin!r} -- install it from https://exiftool.org/"
        )

    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()


def run(
    album: str,
    source_dir: Path,
    suspect_after: str,
    dry_run: bool,
    *,
    osxphotos_bin: str = "osxphotos",
    exiftool_bin: str = DEFAULT_EXIFTOOL_BIN,
    query_fn: QueryFn = _default_query_fn,
    tag_reader_fn: Optional[TagReaderFn] = None,
    date_setter_fn: DateSetterFn = _default_date_setter_fn,
    which_fn: WhichFn = shutil.which,
    platform_fn: PlatformFn = lambda: sys.platform,
) -> RunSummary:
    summary = RunSummary(stage="photos-fix-dates", dry_run=dry_run)

    result = preflight(source_dir, osxphotos_bin, exiftool_bin, which_fn, platform_fn)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    # Bound to exiftool_bin only when the caller didn't supply their own
    # tag_reader_fn (tests always supply one, so this branch is exercised
    # only by real CLI usage).
    if tag_reader_fn is None:
        tag_reader_fn = functools.partial(_default_tag_reader_fn, exiftool_bin=exiftool_bin)

    suspect_after_dt = datetime.strptime(suspect_after, "%Y-%m-%d")
    items = query_fn(osxphotos_bin, album, suspect_after)
    summary.bump("queried", len(items))

    fixed = 0
    skipped = 0
    for item in items:
        fname = item.get("original_filename")
        uuid = item.get("uuid")
        if not fname or not uuid:
            skipped += 1
            summary.note(f"SKIPPED: {item} -- missing filename/uuid in query result")
            continue

        path = source_dir / fname
        if not path.is_file():
            skipped += 1
            summary.note(f"SKIPPED: {fname} -- source file not found at {path}")
            continue

        reading = read_exif_date(path, suspect_after_dt, tag_reader_fn)
        if reading is None:
            skipped += 1
            summary.note(f"SKIPPED: {fname} -- no readable date in any of {DATE_TAGS}")
            continue

        exif_date, tag_used = reading
        warning = ""
        if exif_date >= suspect_after_dt:
            warning = f" ** WARNING: even {tag_used} is still >= {suspect_after} -- check by hand, may not be a real fix **"

        old_date = item.get("date")
        if dry_run:
            summary.note(f"[dry-run] {fname}: {old_date} -> {exif_date} (from {tag_used}){warning}")
            fixed += 1
            continue

        date_setter_fn(uuid, exif_date)
        summary.note(f"{fname}: {old_date} -> {exif_date} (from {tag_used}){warning}")
        fixed += 1

    summary.bump("would_fix" if dry_run else "fixed", fixed)
    summary.bump("skipped", skipped)

    return summary
