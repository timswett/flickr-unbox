#!/usr/bin/env python3
"""
build_private_test_fixture.py -- build a small, PRIVATE test fixture from
your own real Flickr export.

Two reasons to reach for this:
  1. **Play with flickr-unbox on a small, fast subset first.** Rather than
     pointing the real pipeline at your whole library on the first try,
     build a fixture from one or more photo-batch zips and see the actual
     commands run end to end on real data you own -- this script prints
     the exact `flickr-unbox` command sequence to try next once it's done
     (see "Next steps" in the output).
  2. **Validate a change you're making to this fork against real-world
     filenames/metadata**, not just the synthetic cases in `test_data/`.
     This is how every stage of the Python port was validated against
     real data during development.

Multiple photo/video zips (a real full export arrives as several --
`data-download-1.zip`, `data-download-2.zip`, ...) are handled the same
way production does it: each is extracted into its own subfolder, then
`flickr_unbox.flatten.run()` -- the real `flatten` pipeline stage, not a
second copy of its logic -- collapses them into one flat `photos/` dir,
collision-prefixing any filename that collides across batches. That means
a multi-zip fixture already reflects a real `flatten` run before you ever
get to "Next steps" below, which still correctly starts at
`merge-photoinfo`.

Reuses the pipeline's own matching logic:
    `match_photos_to_json.pl` (the version this replaces) carried its own
    hand-maintained copy of the ID-matching rules -- the same risk that
    `_collision_merge.py` was factored out to avoid elsewhere in this
    port. This script instead imports `flickr_unbox.rename_plan.resolve_id`
    and `.photo_json_id` directly, so the fixture it builds is guaranteed
    to reflect the real pipeline's actual matching behavior, not a second
    copy of it that could quietly drift out of sync.

LOCAL / PRIVATE USE ONLY -- output contains your real photo/EXIF data.
Never commit the output directory; `.gitignore` already excludes
`private_test_output/` and `*_private/` by default, but any --work-dir you
choose outside those patterns is on you to keep private.

Usage:
  python tools/build_private_test_fixture.py <photo_source> [<photo_source> ...] <info_zips_dir> <work_dir>

  Each <photo_source> is a photo/video export zip OR a directory containing
  one or more of them (matched with --photo-glob, default
  "data-download-*.zip" -- Flickr's real export naming). <info_zips_dir> is
  a directory containing one or more JSON info-zip files.

  <photo_source> and <info_zips_dir> can point at the SAME directory: info
  zips are found by globbing "*.zip" in that directory and excluding
  anything that matches --photo-glob, so the photo zips living alongside
  them are never mistaken for info zips. No special folder layout is
  required -- point both at wherever you downloaded everything and go.

  Example, given one directory holding both the "data-download-N.zip"
  photo/video batches and the "*_part*.zip" info batches:

    python tools/build_private_test_fixture.py flickrdata-tim/ flickrdata-tim/ work_dir/
"""
from __future__ import annotations

import argparse
import fnmatch
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

# Runnable directly from a fresh clone without `pip install -e .` first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flickr_unbox import flatten  # noqa: E402
from flickr_unbox._collision_merge import is_junk  # noqa: E402
from flickr_unbox._report import RunSummary  # noqa: E402
from flickr_unbox.rename_plan import photo_json_id, resolve_id  # noqa: E402


def extract_zip(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)


def sweep_junk(root: Path) -> int:
    junk = [p for p in root.rglob("*") if p.is_file() and is_junk(p)]
    for p in junk:
        p.unlink()
    return len(junk)


def resolve_photo_zips(sources: List[Path], photo_glob: str) -> List[Path]:
    """
    Expand each photo_source (a zip file, or a directory to glob within)
    into a flat, deduplicated list of zip paths, in the order given.
    Raises FileNotFoundError if a source doesn't exist at all.
    """
    zips: List[Path] = []
    for source in sources:
        if source.is_dir():
            found = sorted(source.glob(photo_glob))
            if not found:
                print(f"WARNING: no zips matching {photo_glob!r} found in directory {source}")
            zips.extend(found)
        elif source.is_file():
            zips.append(source)
        else:
            raise FileNotFoundError(f"photo source not found: {source}")

    seen: set = set()
    deduped: List[Path] = []
    for z in zips:
        resolved = z.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(z)
    return deduped


def index_json_sidecars(search_dirs: List[Path]) -> Dict[str, Path]:
    """id -> path to its photo_<id>.json, across every search dir."""
    index: Dict[str, Path] = {}
    for d in search_dirs:
        for p in d.rglob("*.json"):
            json_id = photo_json_id(p.name)
            if json_id:
                index[json_id] = p
    return index


def match_photos(
    photos_dir: Path, json_index: Dict[str, Path]
) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """
    Returns (matches, unresolved): matches is [(photo_filename, id,
    source_json_path), ...]; unresolved is a list of photo filenames with
    no matching sidecar. Uses the exact same resolve_id() the real
    pipeline's rename-plan stage uses.
    """
    json_ids = set(json_index.keys())
    matches = []
    unresolved = []
    for p in sorted(photos_dir.iterdir()):
        if not p.is_file() or is_junk(p):
            continue
        found_id = resolve_id(p.name, json_ids)
        if found_id is None:
            unresolved.append(p.name)
        else:
            matches.append((p.name, found_id, str(json_index[found_id])))
    return matches, unresolved


def write_match_report(report_path: Path, matches, unresolved) -> None:
    with open(report_path, "w") as f:
        f.write("status\tphoto_file\tmatched_id\tjson_source\n")
        for photo_file, matched_id, json_source in matches:
            f.write(f"OK\t{photo_file}\t{matched_id}\t{json_source}\n")
        for photo_file in unresolved:
            f.write(f"UNRESOLVED\t{photo_file}\t\t\n")


def print_next_steps(work_dir: Path, photo_zip_count: int) -> None:
    photos_dir = work_dir / "photos"
    matched_json_dir = work_dir / "matched_json"
    print()
    if photo_zip_count > 1:
        print(f"({photo_zip_count} photo/video zips were already merged via flatten -- see the")
        print(" flatten_* counts above. photos/ is the post-flatten state.)")
        print()
    print("Next steps -- try the real pipeline on this fixture:")
    print(f"  1. flickr-unbox merge-photoinfo {work_dir} {photos_dir} \\")
    print(f"       --source-glob {matched_json_dir.name!r} --no-dry-run")
    print(f"       (merges {matched_json_dir.name}/ into {photos_dir.name}/, matching the real")
    print("        pipeline's post-flatten state)")
    print(f"  2. flickr-unbox rename-plan {photos_dir} --plan-path {work_dir}/rename_plan.tsv --no-dry-run")
    print(f"  3. flickr-unbox rename {photos_dir} --plan-path {work_dir}/rename_plan.tsv --no-dry-run")
    print(f"  4. flickr-unbox gps-fix {photos_dir} --no-dry-run")
    print(f"  5. flickr-unbox exif-batches {photos_dir} --batch-dir {work_dir}/batches --no-dry-run")
    print(f"  6. flickr-unbox exif-write {photos_dir} 01 --batch-dir {work_dir}/batches --no-dry-run")
    print(f"  7. flickr-unbox cleanup {photos_dir} 01 --batch-dir {work_dir}/batches --no-dry-run")
    print()
    print("  Every command above defaults to a dry run -- drop --no-dry-run first")
    print("  to preview what each step would do before it touches anything.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "photo_sources",
        type=Path,
        nargs="+",
        help="One or more photo/video export zips, and/or directories containing them "
        "(directories are searched non-recursively via --photo-glob). Must come before "
        "info_zips_dir and work_dir.",
    )
    parser.add_argument("info_zips_dir", type=Path, help="Directory containing one or more JSON info-zip files")
    parser.add_argument("work_dir", type=Path, help="Output directory (created if missing) -- keep this private")
    parser.add_argument(
        "--photo-glob",
        default="data-download-*.zip",
        help="Glob used when a photo_sources entry is a directory, and to exclude photo "
        "zips when scanning info_zips_dir for info zips (default: 'data-download-*.zip', "
        "Flickr's real photo/video export naming)",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    summary = RunSummary(stage="build-private-test-fixture", dry_run=False)

    try:
        photo_zips = resolve_photo_zips(args.photo_sources, args.photo_glob)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1
    if not photo_zips:
        print("ERROR: no photo/video zip files found across the given photo_sources")
        return 1

    info_zips = sorted(
        p for p in args.info_zips_dir.glob("*.zip") if not fnmatch.fnmatch(p.name, args.photo_glob)
    )
    if not info_zips:
        print(f"ERROR: no info .zip files found in {args.info_zips_dir} (after excluding --photo-glob matches)")
        return 1

    photos_dir = args.work_dir / "photos"
    photo_extracted_dir = args.work_dir / "photo_extracted"
    info_extracted_dir = args.work_dir / "info_extracted"
    matched_json_dir = args.work_dir / "matched_json"
    matched_json_dir.mkdir(parents=True, exist_ok=True)

    for zip_path in photo_zips:
        stage_dir = photo_extracted_dir / zip_path.stem
        print(f"Extracting {zip_path.name} -> {stage_dir}")
        extract_zip(zip_path, stage_dir)
    summary.bump("photo_zips_extracted", len(photo_zips))

    print(f"Flattening {len(photo_zips)} extracted photo batch(es) -> {photos_dir}")
    flatten_summary = flatten.run(photo_extracted_dir, photos_dir, source_glob="*", dry_run=False)
    for key, value in flatten_summary.counts.items():
        summary.bump(f"flatten_{key}", value)
    for note in flatten_summary.notes:
        summary.note(f"flatten: {note}")
    shutil.rmtree(photo_extracted_dir, ignore_errors=True)

    part_dirs = []
    for i, zip_path in enumerate(info_zips, start=1):
        part_dir = info_extracted_dir / f"part{i}"
        print(f"Extracting {zip_path.name} -> {part_dir}")
        extract_zip(zip_path, part_dir)
        part_dirs.append(part_dir)
    summary.bump("info_zips_extracted", len(info_zips))
    summary.bump("junk_swept_from_info", sweep_junk(info_extracted_dir))

    json_index = index_json_sidecars(part_dirs)
    summary.bump("json_sidecars_indexed", len(json_index))

    matches, unresolved = match_photos(photos_dir, json_index)
    summary.bump("matched", len(matches))
    summary.bump("unresolved", len(unresolved))

    for _photo_file, matched_id, json_source in matches:
        shutil.copy2(Path(json_source), matched_json_dir / f"photo_{matched_id}.json")

    report_path = args.work_dir / "match_report.tsv"
    write_match_report(report_path, matches, unresolved)

    mini_infozip_path = args.work_dir / "mini_infozip.zip"
    with zipfile.ZipFile(mini_infozip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in matched_json_dir.iterdir():
            zf.write(p, arcname=p.name)

    summary.note(f"photos: {photos_dir}")
    summary.note(f"matched JSON: {matched_json_dir}")
    summary.note(f"mini info-zip: {mini_infozip_path}")
    summary.note(f"match report: {report_path}")
    summary.print()

    print()
    print("REMINDER: this folder contains real personal photo/EXIF data.")
    print("Do not commit it -- local use only.")

    print_next_steps(args.work_dir, len(photo_zips))

    return 0


if __name__ == "__main__":
    sys.exit(main())
