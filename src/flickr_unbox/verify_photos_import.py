"""
photos-verify -- rigorously verify which files from an intended import
list actually made it into Apple Photos, by diffing the full intended
filename set against every possible logged outcome in a
`photos_import.py` log file, rather than trusting a chunk's `rc=0` or a
naive processed-file counter (see `photos_import.py`'s module docstring,
lessons 4 and 5, for why those are not reliable completeness signals).

Ports `verify_import_completeness.sh`. This exact reconciliation method
caught real, otherwise-invisible data loss in production: a chunk that
auto-recovered internally (`rc=0`) after silently abandoning most of its
files, and osxphotos's burst-group handling silently dropping files with
zero log output at all.

IMPORTANT: osxphotos's `-V` output embeds raw ANSI color escape codes
directly around filenames/UUIDs in "Added"/"Imported" lines, even when
redirected to a file. A naive regex against the raw log silently returns
wrong/zero results -- `_osxphotos.strip_ansi()` is applied to the whole
log before any parsing here; don't remove that step or re-introduce a
raw-log match.

Read-only, report-generating stage (like `doctor.py`) with no
`--no-dry-run` concept -- its only side effect is writing analysis files
into `out_dir`, which is the entire point of running it.

No real captured import log sample exists in this repo -- the patterns
below are a faithful 1:1 translation of the shell script's
`grep`/`awk`/`sed` patterns, which were validated against real production
logs in the original migration (reproduced the exact real counts, zero
discrepancy from manual analysis), but not independently re-validated
against a fresh real log here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set

from . import _osxphotos
from ._report import RunSummary

# Ported from verify_import_completeness.sh's grep/awk/sed patterns --
# no exact real log line sample for "Skipping duplicate" is available in
# this repo (see module docstring), so that one is translated literally
# as "3rd whitespace-separated field of a matching line" (awk '{print $3}')
# rather than guessing a specific line shape via a regex capture group.
# Assumed shape (matching test fixtures below): "Skipping duplicate
# <filename> ..." -- if a real captured log ever shows a different shape
# for this line, re-derive the field index from that instead of trusting
# this comment.
_ADDED_RE = re.compile(r"^Added (\S+)", re.MULTILINE)
_IMPORTED_RE = re.compile(r"^Imported (\S+)", re.MULTILINE)
_ERROR_RE = re.compile(r"Error importing file (\S+)")
_DUPSKIPPED_LINE_RE = re.compile(r"^Skipping duplicate.*$", re.MULTILINE)


@dataclass
class LogOutcomes:
    added: Set[str] = field(default_factory=set)
    imported: Set[str] = field(default_factory=set)
    errors: Set[str] = field(default_factory=set)
    dupskipped: Set[str] = field(default_factory=set)

    @property
    def orphans(self) -> Set[str]:
        # Imported (got a UUID) but never reached the "Added ... to album"
        # step -- usually means a crash interrupted that exact file
        # mid-processing (see photos_import.py lesson 4).
        return self.imported - self.added

    @property
    def accounted_for(self) -> Set[str]:
        return self.added | self.orphans | self.errors | self.dupskipped


def load_intended(paths_file: Path) -> Set[str]:
    """Basenames of every line in paths_file -- accepts either full paths
    (as import_batch.sh's original input took) or bare basenames."""
    with open(paths_file) as f:
        return {Path(line.strip()).name for line in f if line.strip()}


def _third_field(line: str) -> str:
    """Python equivalent of awk '{print $3}' -- split on whitespace, take
    the 3rd token. Returns "" if the line has fewer than 3 fields (won't
    happen for a line that matched _DUPSKIPPED_LINE_RE, which requires at
    least "Skipping duplicate", but stays defensive rather than raising)."""
    parts = line.split()
    return parts[2] if len(parts) >= 3 else ""


def parse_log(clean_log_text: str) -> LogOutcomes:
    """clean_log_text must already have ANSI escapes stripped -- see
    _osxphotos.strip_ansi(). Not done inside this function so a caller
    that already has clean text (e.g. a test fixture) doesn't pay to
    strip twice."""
    dupskipped = {
        _third_field(line) for line in _DUPSKIPPED_LINE_RE.findall(clean_log_text)
    } - {""}
    return LogOutcomes(
        added=set(_ADDED_RE.findall(clean_log_text)),
        imported=set(_IMPORTED_RE.findall(clean_log_text)),
        errors=set(_ERROR_RE.findall(clean_log_text)),
        dupskipped=dupskipped,
    )


def _write_lines(path: Path, names: Set[str]) -> None:
    with open(path, "w") as f:
        for name in sorted(names):
            f.write(f"{name}\n")


def run(intended_files_list: Path, log_file: Path, out_dir: Path) -> RunSummary:
    summary = RunSummary(stage="photos-verify", dry_run=False)

    if not intended_files_list.is_file():
        summary.note(f"PREFLIGHT FAILED: intended files list not found: {intended_files_list}")
        summary.bump("preflight_failed")
        return summary
    if not log_file.is_file():
        summary.note(f"PREFLIGHT FAILED: log file not found: {log_file}")
        summary.bump("preflight_failed")
        return summary

    out_dir.mkdir(parents=True, exist_ok=True)

    intended = load_intended(intended_files_list)
    clean_log = _osxphotos.strip_ansi(log_file.read_text())
    outcomes = parse_log(clean_log)
    missing = intended - outcomes.accounted_for

    _write_lines(out_dir / "intended.txt", intended)
    _write_lines(out_dir / "added.txt", outcomes.added)
    _write_lines(out_dir / "orphans.txt", outcomes.orphans)
    _write_lines(out_dir / "errors.txt", outcomes.errors)
    _write_lines(out_dir / "dupskipped.txt", outcomes.dupskipped)
    _write_lines(out_dir / "missing.txt", missing)

    summary.bump("intended", len(intended))
    summary.bump("added", len(outcomes.added))
    summary.bump("orphans", len(outcomes.orphans))
    summary.bump("errors", len(outcomes.errors))
    summary.bump("dupskipped", len(outcomes.dupskipped))
    summary.bump("missing", len(missing))

    summary.note(f"output written to {out_dir}")
    if missing:
        summary.note(
            "MISSING files have no outcome logged at all -- re-import them via "
            "photos-retry ONE AT A TIME, not batched (batching former burst-siblings "
            "re-triggers the burst bug against each other, see photos_import.py lesson 5)"
        )

    return summary
