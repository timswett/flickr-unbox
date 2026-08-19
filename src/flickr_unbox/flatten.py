"""
flatten -- collapse a Flickr export's per-zip folders into one flat directory.

Purpose:
    A full Flickr account export arrives as dozens of separate zip archives
    (extracted here into e.g. `data-download-01/`, `data-download-02/`, ...),
    each holding a subset of your photos/videos in its own nested folder
    structure. This stage flattens all of them into one directory so later
    stages (rename-plan, gps-fix, exif-write) only ever have to deal with a
    single flat folder, not N different source trees.

Edge case this exists to handle:
    Two different zip archives can legitimately contain a file with the same
    name (Flickr doesn't guarantee filename uniqueness across export
    batches). A naive flatten would silently overwrite one with the other.
    Instead, on a name collision the *second* file to claim a given target
    name gets prefixed with its source folder's name (e.g.
    `data-download-03_IMG_1234.jpg`) rather than clobbering the first.
    Collisions are resolved this way, not treated as failures -- the only
    real failure case is a collision *on the collision* (the prefixed name
    is also already taken), which is rare enough in practice that it's
    logged and skipped rather than aborting the whole run (see
    CONTRIBUTING.md "Batch operations log-and-continue").

Safety model:
    Defaults to a dry run: computes the full move plan (and the junk-file
    sweep list) without touching anything on disk, and prints a summary.
    Pass --no-dry-run to actually move files; that re-runs the same
    planning pass against current on-disk state first, so a stale plan from
    an earlier dry-run is never assumed to still be valid.
"""
from __future__ import annotations

from pathlib import Path

from . import _collision_merge
from ._preflight import check_source_and_dest
from ._report import RunSummary


def run(source_base: Path, dest: Path, source_glob: str, dry_run: bool) -> RunSummary:
    summary = RunSummary(stage="flatten", dry_run=dry_run)

    result = check_source_and_dest(source_base, dest)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    junk = _collision_merge.find_junk(source_base)
    summary.bump("junk_files_found", len(junk))
    if not dry_run:
        for j in junk:
            j.unlink()

    moves, conflicts = _collision_merge.plan_moves(
        source_base, dest, source_glob, file_predicate=lambda _p: True
    )
    summary.bump("files_to_move" if dry_run else "planned", len(moves))
    summary.bump("collision_renamed", sum(1 for _, _, renamed in moves if renamed))
    summary.bump("unresolved_conflicts", len(conflicts))
    for src, reason in conflicts:
        summary.note(f"CONFLICT (skipped): {src} -- {reason}")

    if dry_run:
        return summary

    dest.mkdir(parents=True, exist_ok=True)
    _collision_merge.execute_moves(moves, summary)
    summary.bump("empty_source_dirs_removed", _collision_merge.remove_empty_source_dirs(source_base, source_glob))

    return summary
