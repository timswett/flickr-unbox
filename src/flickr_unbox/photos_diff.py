"""
photos-diff -- compare a flattened, EXIF-restored directory against the
current Apple Photos library and bucket every file into confirmed-already-
there / needs-manual-review / truly-missing / no-usable-timestamp.

This is new code, not a port. `photos-import` needs an explicit file list
to act on, but nothing generalized ever produced one -- that step
previously only existed as ad hoc analysis (see
`Apple_Flickr_PhotosMatch.md` in the flickrmove repo for the full story).
It originally used PhotoSweeper (a third-party visual-similarity dedup
tool) to find candidate "missing" files, but PhotoSweeper's own matching
turned out to significantly under-match -- many files it called missing
already had an Apple Photos item at the same capture instant, just
visually different enough (recompression, format conversion) to fool
similarity matching. The method that actually held up was
EXIF-capture-timestamp cross-referencing against the Photos library via
osxphotos, which doesn't need PhotoSweeper (or any third-party tool) at
all -- this module reimplements that method directly.

Two-tier window:
  - within `tight_window_s` (default 5s) of some Photos library item ->
    confirmed already there.
  - not tight, but within `loose_window_s` (default 120s) -> needs manual
    review (could be a genuine near-duplicate, or a coincidence).
  - neither -> truly missing -- the list `photos-import --files-list`
    should be pointed at.
  - no usable embedded timestamp (neither DateTimeOriginal nor
    CreateDate) -> can't be matched this way, needs a human to check by
    hand (rare in practice, and consistently video containers whose date
    tags exiftool doesn't reliably populate).

Dry-run behavior follows exif_batches.py's precedent (a *planning* stage
that always computes and reports real counts), not doctor.py's/
verify_photos_import.py's (no dry-run concept at all): there's nothing to
preview without actually doing the real osxphotos/exiftool queries and
the real matching, so a dry run does that in full and only skips writing
the four output files.

No real captured `osxphotos query --json` sample exists in this repo --
the JSON field names used here (`uuid`, `date`, `original_filename`) come
from the documented method and `fix_media_dates.py`'s already-working
`query_album` call, not an independently re-verified fresh query. This is
the stage most worth a real-data validation pass before trusting at scale
(see CLAUDE.md "Two-tier testing").
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import _osxphotos
from ._preflight import PreflightResult
from ._report import RunSummary

DEFAULT_TIGHT_WINDOW_S = 5
DEFAULT_LOOSE_WINDOW_S = 120

# Mirrors exif_batches.py's _TARGET_EXT_RE allowlist (kept as a separate
# copy, not imported, since that name is module-private there and the two
# stages' extension lists are allowed to drift independently if a future
# format needs handling differently for diffing vs. exif-writing).
_TARGET_EXT_RE = re.compile(r"\.(jpe?g|png|gif|heic|heif|mov|mp4)$", re.IGNORECASE)

# (argv, cwd) -> (combined stdout+stderr text, return code)
ProcessRunner = Callable[[List[str], Path], "Tuple[str, int]"]
WhichFn = _osxphotos.WhichFn
PlatformFn = _osxphotos.PlatformFn


@dataclass
class PhotoRecord:
    uuid: str
    date: datetime
    original_filename: Optional[str]


@dataclass
class MatchResult:
    status: str  # "confirmed" | "needs_review" | "missing"
    uuid: Optional[str] = None
    delta_seconds: Optional[float] = None


def _default_process_runner(cmd: List[str], cwd: Path) -> Tuple[str, int]:
    # List-form argv, no shell=True; cmd is built from a fixed binary name
    # (a CLI flag/default, never file/data content) plus our own arguments.
    result = subprocess.run(  # nosec B603
        cmd, cwd=cwd, capture_output=True, text=True
    )
    return (result.stdout or "") + (result.stderr or ""), result.returncode


# --- Apple Photos library side ---


def build_library_query_command(osxphotos_bin: str) -> List[str]:
    return [osxphotos_bin, "query", "--json"]


def _parse_photos_date(raw: str) -> Optional[datetime]:
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Compared against exiftool's naive local-capture-time reading below --
    # both sides represent "what time did the camera's clock say", not a
    # true UTC instant, so stripping tzinfo here (rather than converting)
    # is what makes the two comparable at all.
    return dt.replace(tzinfo=None)


def parse_library_records(items: List[dict]) -> Tuple[List[PhotoRecord], int]:
    """Returns (records sorted by date ascending, count skipped for a
    missing/unparseable uuid or date)."""
    records: List[PhotoRecord] = []
    skipped = 0
    for item in items:
        uuid = item.get("uuid")
        date_raw = item.get("date")
        if not uuid or not date_raw:
            skipped += 1
            continue
        dt = _parse_photos_date(date_raw)
        if dt is None:
            skipped += 1
            continue
        records.append(PhotoRecord(uuid=uuid, date=dt, original_filename=item.get("original_filename")))
    records.sort(key=lambda r: r.date)
    return records, skipped


def load_photos_library(
    osxphotos_bin: str, process_runner: ProcessRunner
) -> Tuple[List[PhotoRecord], int]:
    cmd = build_library_query_command(osxphotos_bin)
    stdout, returncode = process_runner(cmd, Path("."))
    if returncode != 0:
        raise RuntimeError(f"osxphotos query failed (exit {returncode}): {stdout[-2000:]}")
    items = json.loads(stdout)
    return parse_library_records(items)


# --- Flickr-side source files ---


def build_source_dates_command(exiftool_bin: str, argfile_path: Path) -> List[str]:
    return [exiftool_bin, "-j", "-DateTimeOriginal", "-CreateDate", "-@", str(argfile_path.resolve())]


def _parse_exiftool_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def parse_source_dates_json(text: str) -> Dict[str, Optional[datetime]]:
    """basename -> best available date, DateTimeOriginal falling back to
    CreateDate (same fallback order Session 2's method validated), or None
    if neither tag is present/parseable for that file."""
    result: Dict[str, Optional[datetime]] = {}
    for entry in json.loads(text):
        source = entry.get("SourceFile")
        if not source:
            continue
        name = Path(source).name
        dt = _parse_exiftool_date(entry.get("DateTimeOriginal")) or _parse_exiftool_date(entry.get("CreateDate"))
        result[name] = dt
    return result


def read_source_dates(
    dest: Path,
    filenames: List[str],
    exiftool_bin: str,
    process_runner: ProcessRunner,
) -> Dict[str, Optional[datetime]]:
    """
    The argfile exiftool's -@ flag needs to read from is scratch plumbing,
    not a result -- written to a real OS temp file (not under `out_dir`)
    and always cleaned up here, so this function (and therefore a dry-run
    call to run()) never creates or touches out_dir at all. Only run()'s
    real (--no-dry-run) branch writes anything to out_dir.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".txt", prefix="flickr-unbox-photos-diff-")
    argfile_path = Path(tmp_name)
    try:
        with open(fd, "w") as f:
            f.write("\n".join(filenames) + "\n")

        cmd = build_source_dates_command(exiftool_bin, argfile_path)
        stdout, returncode = process_runner(cmd, dest)
        if returncode != 0:
            raise RuntimeError(f"exiftool batch date read failed (exit {returncode}): {stdout[-2000:]}")
    finally:
        argfile_path.unlink(missing_ok=True)

    dates = parse_source_dates_json(stdout)
    # Guarantee every requested filename gets bucketed even if exiftool's
    # own output omitted an entry outright (shouldn't happen for an
    # existing file, but silently dropping it from downstream bucketing
    # would be worse than surfacing it as "no timestamp").
    for name in filenames:
        dates.setdefault(name, None)
    return dates


# --- matching ---


def match(
    source_date: datetime,
    sorted_library: List[PhotoRecord],
    sorted_dates: List[datetime],
    tight_s: float = DEFAULT_TIGHT_WINDOW_S,
    loose_s: float = DEFAULT_LOOSE_WINDOW_S,
) -> MatchResult:
    """
    Nearest-neighbor lookup via bisect against a library already sorted by
    date -- `sorted_dates` is `[r.date for r in sorted_library]`, passed in
    (rather than rebuilt here) so a caller matching many source files
    against the same library only pays that O(n) cost once, not once per
    file -- matters at real scale (tens of thousands of files each way).
    """
    if not sorted_library:
        return MatchResult(status="missing")

    idx = bisect_left(sorted_dates, source_date)
    candidates = []
    if idx < len(sorted_library):
        candidates.append(sorted_library[idx])
    if idx > 0:
        candidates.append(sorted_library[idx - 1])

    best = min(candidates, key=lambda r: abs((r.date - source_date).total_seconds()))
    delta = (best.date - source_date).total_seconds()
    abs_delta = abs(delta)

    if abs_delta <= tight_s:
        return MatchResult(status="confirmed", uuid=best.uuid, delta_seconds=delta)
    if abs_delta <= loose_s:
        return MatchResult(status="needs_review", uuid=best.uuid, delta_seconds=delta)
    return MatchResult(status="missing")


# --- run ---


def _media_files(dest: Path) -> List[str]:
    return sorted(
        p.name
        for p in dest.iterdir()
        if p.is_file() and not p.name.startswith("._") and _TARGET_EXT_RE.search(p.name)
    )


def preflight(
    dest: Path,
    osxphotos_bin: str,
    exiftool_bin: str,
    which_fn: WhichFn,
    platform_fn: PlatformFn,
) -> PreflightResult:
    if not dest.is_dir():
        return PreflightResult.failed(f"dest does not exist or is not a directory: {dest}")

    reasons = []
    platform_result = _osxphotos.preflight_platform_and_binary(osxphotos_bin, which_fn, platform_fn)
    if not platform_result.ok:
        reasons.extend(platform_result.reasons)
    if which_fn(exiftool_bin) is None:
        reasons.append(
            f"exiftool binary not found on PATH: {exiftool_bin!r} -- install it from https://exiftool.org/"
        )
    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()


def _write_tsv(path: Path, rows: List[Tuple[str, str, float]]) -> None:
    with open(path, "w") as f:
        for name, uuid, delta in rows:
            f.write(f"{name}\t{uuid}\t{delta:g}\n")


def _write_lines(path: Path, names: List[str]) -> None:
    with open(path, "w") as f:
        for name in names:
            f.write(f"{name}\n")


def run(
    dest: Path,
    dry_run: bool,
    *,
    out_dir: Path,
    tight_window_s: float = DEFAULT_TIGHT_WINDOW_S,
    loose_window_s: float = DEFAULT_LOOSE_WINDOW_S,
    osxphotos_bin: str = "osxphotos",
    exiftool_bin: str = "exiftool",
    process_runner: ProcessRunner = _default_process_runner,
    which_fn: WhichFn = shutil.which,
    platform_fn: PlatformFn = lambda: sys.platform,
) -> RunSummary:
    summary = RunSummary(stage="photos-diff", dry_run=dry_run)

    result = preflight(dest, osxphotos_bin, exiftool_bin, which_fn, platform_fn)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    filenames = _media_files(dest)
    summary.bump("source_files", len(filenames))
    if not filenames:
        summary.note("no media files found in dest -- nothing to diff")
        return summary

    library, lib_skipped = load_photos_library(osxphotos_bin, process_runner)
    summary.bump("library_items", len(library))
    if lib_skipped:
        summary.note(
            f"{lib_skipped} Photos library item(s) skipped (missing/unparseable uuid or date)"
        )
    sorted_dates = [r.date for r in library]

    dates = read_source_dates(dest, filenames, exiftool_bin, process_runner)

    confirmed: List[Tuple[str, str, float]] = []
    needs_review: List[Tuple[str, str, float]] = []
    missing: List[str] = []
    no_timestamp: List[str] = []

    for name in filenames:
        source_date = dates.get(name)
        if source_date is None:
            no_timestamp.append(name)
            continue
        m = match(source_date, library, sorted_dates, tight_window_s, loose_window_s)
        if m.status == "confirmed":
            confirmed.append((name, m.uuid, m.delta_seconds))
        elif m.status == "needs_review":
            needs_review.append((name, m.uuid, m.delta_seconds))
        else:
            missing.append(name)

    summary.bump("confirmed", len(confirmed))
    summary.bump("needs_review", len(needs_review))
    summary.bump("truly_missing", len(missing))
    summary.bump("no_exif_timestamp", len(no_timestamp))

    if dry_run:
        summary.note("output files not written (dry run)")
        return summary

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_tsv(out_dir / "confirmed.txt", confirmed)
    _write_tsv(out_dir / "needs_review.txt", needs_review)
    _write_lines(out_dir / "truly_missing.txt", missing)
    _write_lines(out_dir / "no_exif_timestamp.txt", no_timestamp)
    summary.note(
        f"truly_missing.txt written to {out_dir / 'truly_missing.txt'} "
        "-- feed this to `photos-import --files-list`"
    )

    return summary
