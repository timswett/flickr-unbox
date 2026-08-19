"""
exif-batches -- split a flattened directory's photo/video files into
size-based batches for exif-write.

Purpose:
    exiftool creates a full `_original` backup of every file it writes to
    by default. Doing the whole library in one pass would need free disk
    space equal to the entire library's size -- for a real ~300GB export
    with far less headroom than that, the write has to be batched. This
    stage splits target files into size-based batches (default ~40GB
    each) so `exif-write` can process, and `cleanup` can free up, one
    batch at a time rather than needing the whole library's worth of
    backup space at once.

Read-only planning step:
    Like `rename-plan`, this never touches a real photo/video file -- it
    only lists them and writes `batch_NN.txt` files (one filename per
    line) for `exif-write` to consume. There's no "is the plan stale"
    check the way `rename.py` needs one: this stage always recomputes
    fresh from dest's current contents on every run, so there's nothing
    pre-existing to go stale against.

Deliberate addition beyond `build_exif_batches.pl`:
    The bash/perl version just overwrites `batch_01.txt`..`batch_NN.txt`
    for whatever N the current run produces. If an earlier run (more
    files, or a smaller --batch-size) produced batch_01..batch_12 and this
    run only needs 8, files batch_09..batch_12 are stale leftovers that a
    later `exif-write` run could still be pointed at by mistake. This port
    clears every pre-existing `batch_*.txt` in batch_dir before writing
    the new set, so a stale batch file can never silently survive a
    re-plan.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Tuple

from ._preflight import PreflightResult
from ._report import RunSummary

DEFAULT_BATCH_BYTES = 40 * 1024**3  # 40GB, matches build_exif_batches.pl's default

_TARGET_EXT_RE = re.compile(r"\.(jpe?g|png|gif|mov|mp4)$", re.IGNORECASE)
_BATCH_FILE_RE = re.compile(r"^batch_\d+\.txt$")

Batch = List[Tuple[str, int]]  # [(filename, size_bytes), ...]


def _is_junk(name: str) -> bool:
    return name.startswith("._")


def _target_files(dest: Path) -> List[Tuple[str, int]]:
    """Sorted (filename, size_bytes) for every exiftool-eligible file in dest."""
    targets = []
    for p in sorted(dest.iterdir()):
        if not p.is_file() or _is_junk(p.name) or not _TARGET_EXT_RE.search(p.name):
            continue
        targets.append((p.name, p.stat().st_size))
    return targets


def plan_batches(dest: Path, batch_bytes: int) -> List[Batch]:
    """Split dest's target files into size-based batches, without touching disk."""
    batches: List[Batch] = [[]]
    current_bytes = 0
    for name, size in _target_files(dest):
        if current_bytes > 0 and current_bytes + size > batch_bytes:
            batches.append([])
            current_bytes = 0
        batches[-1].append((name, size))
        current_bytes += size

    if len(batches) == 1 and not batches[0]:
        return []
    return batches


def preflight(dest: Path, batch_dir: Path) -> PreflightResult:
    reasons = []
    if not dest.is_dir():
        reasons.append(f"dest does not exist or is not a directory: {dest}")

    if batch_dir.exists():
        if not batch_dir.is_dir():
            reasons.append(f"batch_dir exists and is not a directory: {batch_dir}")
        elif not os.access(batch_dir, os.W_OK):
            reasons.append(f"batch_dir is not writable: {batch_dir}")
    elif not batch_dir.parent.is_dir():
        reasons.append(f"batch_dir does not exist and its parent doesn't either: {batch_dir.parent}")
    elif not os.access(batch_dir.parent, os.W_OK):
        reasons.append(f"batch_dir's parent is not writable, can't create batch_dir: {batch_dir.parent}")

    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()


def run(dest: Path, batch_dir: Path, batch_bytes: int, dry_run: bool) -> RunSummary:
    summary = RunSummary(stage="exif-batches", dry_run=dry_run)

    result = preflight(dest, batch_dir)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    batches = plan_batches(dest, batch_bytes)
    total_files = sum(len(b) for b in batches)
    total_bytes = sum(size for batch in batches for _name, size in batch)

    summary.bump("target_files", total_files)
    summary.bump("batches", len(batches))
    summary.note(f"total: {total_files} file(s), {total_bytes / 1024**3:.2f} GB across {len(batches)} batch(es)")
    for i, batch in enumerate(batches, start=1):
        batch_size = sum(size for _name, size in batch)
        summary.note(f"batch_{i:02d}.txt: {len(batch)} file(s), {batch_size / 1024**3:.2f} GB")

    if dry_run:
        return summary

    batch_dir.mkdir(parents=True, exist_ok=True)
    stale = [p for p in batch_dir.glob("batch_*.txt") if _BATCH_FILE_RE.match(p.name)]
    for p in stale:
        p.unlink()
    summary.bump("stale_batch_files_removed", len(stale))

    for i, batch in enumerate(batches, start=1):
        batch_path = batch_dir / f"batch_{i:02d}.txt"
        with open(batch_path, "w") as f:
            for name, _size in batch:
                f.write(f"{name}\n")
    summary.note(f"batch files written to {batch_dir}")

    return summary
