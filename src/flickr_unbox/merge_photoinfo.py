"""
merge-photoinfo -- merge Flickr's separately-exported JSON metadata into the
flattened photo/video directory.

Purpose:
    Flickr splits per-photo metadata (`photo_<id>.json` -- title,
    description, capture date, GPS, tags) into its own separate batch of
    zip archives from the actual photo/video files. Once those are
    extracted (into e.g. `PhotoInformation/<album-id>_part2/`,
    `..._part3/`, ...), this stage merges the JSON sidecars into the same
    flat directory `flatten` produced, so every `<photo>.ext` ends up next
    to its `photo_<id>.json`.

Edge case this exists to handle:
    Same collision concern as `flatten` -- a JSON sidecar with the same
    filename could theoretically appear in more than one part folder. Same
    fix: collision-prefix with the source part-folder's name rather than
    overwrite, and a collision-on-the-collision is logged and skipped
    rather than aborting the run. In practice this has never fired on real
    data (0 collisions across ~50K sidecars), but the check stays cheap
    insurance.

Safety model:
    Same as flatten: defaults to a dry run (full validation + summary, no
    writes); --no-dry-run re-validates from current on-disk state and then
    acts.
"""
from __future__ import annotations

from pathlib import Path

from . import _collision_merge
from ._preflight import check_source_and_dest
from ._report import RunSummary


def _is_json(path: Path) -> bool:
    return path.suffix.lower() == ".json"


def run(source_base: Path, dest: Path, source_glob: str, dry_run: bool) -> RunSummary:
    summary = RunSummary(stage="merge-photoinfo", dry_run=dry_run)

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
        source_base, dest, source_glob, file_predicate=_is_json
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

    # Belt-and-suspenders final junk sweep on dest itself (non-recursive),
    # in case Finder/Spotlight regenerated any during the run -- matches
    # merge_photoinfo.sh's final sweep, which flatten_flickr.sh doesn't have.
    dest_junk = sorted(p for p in dest.glob("*") if p.is_file() and _collision_merge.is_junk(p))
    for j in dest_junk:
        j.unlink()
    summary.bump("dest_junk_swept", len(dest_junk))

    return summary
