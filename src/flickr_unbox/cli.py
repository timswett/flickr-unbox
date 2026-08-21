"""
flickr-unbox -- single CLI, one subcommand per pipeline stage.

Every subcommand defaults to a dry run (full validation + summary, nothing
on disk changes) and requires --no-dry-run to act for real; --no-dry-run
re-validates from current on-disk state rather than trusting an earlier
dry-run. This file is deliberately thin, one block per stage -- no
pipeline logic of its own, just argument parsing and dispatch into each
stage's own module.

Every subcommand other than `doctor` itself prints a one-line environment
banner (see doctor.py) before doing anything else, in both dry-run and
--no-dry-run mode -- so a missing `exiftool` surfaces immediately on the
very first command of a workflow, not five stages in when `exif-write`
finally needs it. That banner is informational only; `exif-write`'s own
pre-flight is still the real, blocking gate for that specific stage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (
    cleanup,
    doctor,
    exif_batches,
    exif_write,
    fix_media_dates,
    flatten,
    gps_fix,
    merge_photoinfo,
    photos_diff,
    photos_import,
    rename,
    rename_plan,
    verify_photos_import,
)


def _add_dry_run_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        default=True,
        help="Act for real. Without this flag, only validates and reports what would happen.",
    )


def _add_merge_style_subcommand(
    subparsers: argparse._SubParsersAction,
    name: str,
    help_text: str,
    default_source_glob: str,
    source_glob_help: str,
    module,
) -> None:
    """
    Wires up a subcommand for a stage shaped like "merge N source
    subfolders into one flat dest" (flatten, merge-photoinfo). Both take
    the same (source_base, dest, --source-glob, --no-dry-run) arguments and
    call module.run(...) with the same signature, so this is shared instead
    of writing the same argparse block out twice per stage.
    """
    p = subparsers.add_parser(name, help=help_text)
    p.add_argument("source_base", type=Path, help="Directory containing the source subfolders")
    p.add_argument("dest", type=Path, help="Flat directory to merge everything into")
    p.add_argument(
        "--source-glob",
        default=default_source_glob,
        help=f"{source_glob_help} (default: %(default)s)",
    )
    _add_dry_run_flag(p)
    p.set_defaults(func=lambda args: _run_merge_style(module, args))


def _run_merge_style(module, args: argparse.Namespace) -> int:
    summary = module.run(args.source_base, args.dest, args.source_glob, args.dry_run)
    summary.print()
    return 1 if summary.counts.get("preflight_failed") or summary.counts.get("failed") else 0


def _add_dest_plan_subcommand(
    subparsers: argparse._SubParsersAction,
    name: str,
    help_text: str,
    dest_help: str,
    module,
) -> None:
    """
    Wires up a subcommand shaped like "operate on dest using a plan file"
    (rename-plan, which writes the plan; rename, which reads it). Both
    call module.run(dest, plan_path, dry_run) with the same signature.
    """
    p = subparsers.add_parser(name, help=help_text)
    p.add_argument("dest", type=Path, help=dest_help)
    p.add_argument(
        "--plan-path",
        type=Path,
        default=Path("rename_plan.tsv"),
        help="Rename plan TSV path (default: %(default)s)",
    )
    _add_dry_run_flag(p)
    p.set_defaults(func=lambda args: _run_dest_plan_style(module, args))


def _run_dest_plan_style(module, args: argparse.Namespace) -> int:
    summary = module.run(args.dest, args.plan_path, args.dry_run)
    summary.print()
    return 1 if summary.counts.get("preflight_failed") or summary.counts.get("failed") else 0


def _add_gps_fix(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "gps-fix",
        help="Insert the missing decimal point into GPS coordinates in <id>.json sidecars.",
    )
    p.add_argument("dest", type=Path, help="Directory of renamed <id>.json sidecars to fix")
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_gps_fix)


def _run_gps_fix(args: argparse.Namespace) -> int:
    summary = gps_fix.run(args.dest, args.dry_run)
    summary.print()
    return 1 if summary.counts.get("preflight_failed") or summary.counts.get("failed") else 0


def _add_exif_batches(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "exif-batches",
        help="Split a flattened directory's photo/video files into size-based batches for exif-write.",
    )
    p.add_argument("dest", type=Path, help="Flattened directory to batch")
    p.add_argument(
        "--batch-dir",
        type=Path,
        default=Path("batches"),
        help="Where to write batch_NN.txt files (default: %(default)s)",
    )
    p.add_argument(
        "--batch-size-gb",
        type=float,
        default=exif_batches.DEFAULT_BATCH_BYTES / 1024**3,
        help="Target size per batch in GB (default: %(default)s)",
    )
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_exif_batches)


def _run_exif_batches(args: argparse.Namespace) -> int:
    batch_bytes = int(args.batch_size_gb * 1024**3)
    summary = exif_batches.run(args.dest, args.batch_dir, batch_bytes, args.dry_run)
    summary.print()
    return 1 if summary.counts.get("preflight_failed") else 0


def _add_exif_write(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "exif-write",
        help="Run the validated exiftool command against one batch of files.",
    )
    p.add_argument("dest", type=Path, help="Flattened directory to write EXIF data into")
    p.add_argument("batch_num", help="Batch number matching batch_dir/batch_<NUM>.txt, e.g. 01")
    p.add_argument(
        "--batch-dir",
        type=Path,
        default=Path("batches"),
        help="Directory containing batch_NN.txt files (default: %(default)s)",
    )
    p.add_argument(
        "--exiftool-bin",
        default=exif_write.DEFAULT_EXIFTOOL_BIN,
        help="exiftool executable to invoke (default: %(default)s)",
    )
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_exif_write)


def _run_exif_write(args: argparse.Namespace) -> int:
    summary = exif_write.run(
        args.dest, args.batch_dir, args.batch_num, args.dry_run, exiftool_bin=args.exiftool_bin
    )
    summary.print()
    return 1 if summary.counts.get("preflight_failed") or summary.counts.get("files_errored") else 0


def _add_cleanup(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "cleanup",
        help="Delete a batch's exiftool _original backup files once exif-write is verified clean.",
    )
    p.add_argument("dest", type=Path, help="Flattened directory the backups live in")
    p.add_argument("batch_num", help="Batch number matching batch_dir/batch_<NUM>.txt, e.g. 01")
    p.add_argument(
        "--batch-dir",
        type=Path,
        default=Path("batches"),
        help="Directory containing batch_NN.txt and batch_NN.result.json files (default: %(default)s)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the result-receipt safety gate (missing/errored/stale receipt). "
        "Never bypasses the batch-file/dest existence checks.",
    )
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_cleanup)


def _run_cleanup(args: argparse.Namespace) -> int:
    summary = cleanup.run(args.dest, args.batch_dir, args.batch_num, args.dry_run, force=args.force)
    summary.print()
    return 1 if summary.counts.get("preflight_failed") else 0


def _add_photos_diff(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "photos-diff",
        help="[macOS, optional] Compare dest against your Apple Photos library, "
        "write truly_missing.txt for photos-import.",
    )
    p.add_argument("dest", type=Path, help="Flattened, EXIF-restored directory to diff")
    p.add_argument(
        "--out-dir", type=Path, default=Path("photos_diff_out"),
        help="Where to write confirmed.txt/needs_review.txt/truly_missing.txt/no_exif_timestamp.txt (default: %(default)s)",
    )
    p.add_argument(
        "--tight-window-s", type=float, default=photos_diff.DEFAULT_TIGHT_WINDOW_S,
        help="Seconds within which a timestamp match is confirmed (default: %(default)s)",
    )
    p.add_argument(
        "--loose-window-s", type=float, default=photos_diff.DEFAULT_LOOSE_WINDOW_S,
        help="Seconds within which a timestamp match needs manual review (default: %(default)s)",
    )
    p.add_argument("--osxphotos-bin", default="osxphotos", help="osxphotos executable to invoke (default: %(default)s)")
    p.add_argument("--exiftool-bin", default=exif_write.DEFAULT_EXIFTOOL_BIN, help="exiftool executable to invoke (default: %(default)s)")
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_photos_diff)


def _run_photos_diff(args: argparse.Namespace) -> int:
    summary = photos_diff.run(
        args.dest, args.dry_run,
        out_dir=args.out_dir, tight_window_s=args.tight_window_s, loose_window_s=args.loose_window_s,
        osxphotos_bin=args.osxphotos_bin, exiftool_bin=args.exiftool_bin,
    )
    summary.print()
    return 1 if summary.counts.get("preflight_failed") else 0


def _add_photos_import(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "photos-import",
        help="[macOS, optional] Chunked import of a file list into Apple Photos via osxphotos.",
    )
    p.add_argument("dest", type=Path, help="Directory the listed files live in")
    p.add_argument("--files-list", type=Path, required=True, help="Basenames to import, one per line, relative to dest")
    p.add_argument("--album", required=True, help="Target Photos album (created if missing)")
    p.add_argument("--batch-dir", type=Path, default=Path("photos_import_batch"), help="Where to write the log/report (default: %(default)s)")
    p.add_argument("--chunk-size", type=int, default=photos_import.DEFAULT_CHUNK_SIZE, help="Files per osxphotos import call (default: %(default)s)")
    p.add_argument("--stop-on-error", type=int, default=photos_import.DEFAULT_STOP_ON_ERROR, help="osxphotos --stop-on-error threshold (default: %(default)s)")
    p.add_argument("--osxphotos-bin", default="osxphotos", help="osxphotos executable to invoke (default: %(default)s)")
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_photos_import)


def _run_photos_import(args: argparse.Namespace) -> int:
    summary = photos_import.run(
        args.dest, args.files_list, args.album, args.dry_run,
        batch_dir=args.batch_dir, chunk_size=args.chunk_size, stop_on_error=args.stop_on_error,
        osxphotos_bin=args.osxphotos_bin,
    )
    summary.print()
    return 1 if summary.counts.get("preflight_failed") else 0


def _add_photos_retry(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "photos-retry",
        help="[macOS, optional] Retry straggler files one at a time (never batched -- see photos_import.py).",
    )
    p.add_argument("dest", type=Path, help="Directory the listed files live in")
    p.add_argument("--files-list", type=Path, required=True, help="Basenames to retry, one per line (e.g. photos-verify's errors.txt/missing.txt/orphans.txt)")
    p.add_argument("--album", required=True, help="Target Photos album (created if missing)")
    p.add_argument(
        "--mode", required=True, choices=photos_import.DUP_MODES,
        help="skip-dups for genuine per-file errors; allow-duplicates for missing/orphan entries osxphotos's own bookkeeping already believes exist",
    )
    p.add_argument("--batch-dir", type=Path, default=Path("photos_import_batch"), help="Where to write the log/report (default: %(default)s)")
    p.add_argument("--osxphotos-bin", default="osxphotos", help="osxphotos executable to invoke (default: %(default)s)")
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_photos_retry)


def _run_photos_retry(args: argparse.Namespace) -> int:
    summary = photos_import.retry(
        args.dest, args.files_list, args.album, args.mode, args.dry_run,
        batch_dir=args.batch_dir, osxphotos_bin=args.osxphotos_bin,
    )
    summary.print()
    return 1 if summary.counts.get("preflight_failed") else 0


def _add_photos_verify(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "photos-verify",
        help="[macOS, optional] Reconcile a photos-import/photos-retry log against the intended file list. "
        "No --no-dry-run: this only ever writes report files.",
    )
    p.add_argument("--intended-files-list", type=Path, required=True, help="The file list that was fed to photos-import")
    p.add_argument("--log-file", type=Path, required=True, help="The log file photos-import/photos-retry wrote to")
    p.add_argument("--out-dir", type=Path, default=Path("photos_verify_out"), help="Where to write intended/added/orphans/errors/dupskipped/missing.txt (default: %(default)s)")
    p.set_defaults(func=_run_photos_verify)


def _run_photos_verify(args: argparse.Namespace) -> int:
    summary = verify_photos_import.run(args.intended_files_list, args.log_file, args.out_dir)
    summary.print()
    return 1 if summary.counts.get("preflight_failed") else 0


def _add_photos_fix_dates(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "photos-fix-dates",
        help="[macOS, optional] Fix GIF/PNG (and other source-corrupted-tag) dates Photos mis-imported.",
    )
    p.add_argument("--album", required=True, help="Photos album name to check")
    p.add_argument("--source-dir", type=Path, required=True, help="Directory containing the original source files")
    p.add_argument("--suspect-after", required=True, help="Find photos dated on/after this date, e.g. 2026-08-01")
    p.add_argument("--osxphotos-bin", default="osxphotos", help="osxphotos executable to invoke (default: %(default)s)")
    p.add_argument("--exiftool-bin", default=fix_media_dates.DEFAULT_EXIFTOOL_BIN, help="exiftool executable to invoke (default: %(default)s)")
    _add_dry_run_flag(p)
    p.set_defaults(func=_run_photos_fix_dates)


def _run_photos_fix_dates(args: argparse.Namespace) -> int:
    summary = fix_media_dates.run(
        args.album, args.source_dir, args.suspect_after, args.dry_run,
        osxphotos_bin=args.osxphotos_bin, exiftool_bin=args.exiftool_bin,
    )
    summary.print()
    return 1 if summary.counts.get("preflight_failed") else 0


def _add_doctor(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "doctor",
        help="Check that the local environment has what flickr-unbox needs (mainly: exiftool).",
    )
    p.add_argument(
        "--exiftool-bin",
        default=exif_write.DEFAULT_EXIFTOOL_BIN,
        help="exiftool executable to look for (default: %(default)s)",
    )
    p.add_argument(
        "--osxphotos-bin",
        default="osxphotos",
        help="osxphotos executable to look for on macOS (default: %(default)s)",
    )
    p.set_defaults(func=_run_doctor)


def _run_doctor(args: argparse.Namespace) -> int:
    summary = doctor.run(args.exiftool_bin, osxphotos_bin=args.osxphotos_bin)
    summary.print()
    return 1 if summary.counts.get("missing") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flickr-unbox")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_merge_style_subcommand(
        subparsers,
        "flatten",
        "Collapse a Flickr export's per-zip folders into one flat directory.",
        default_source_glob="data-download-*",
        source_glob_help="Glob (relative to source_base) matching each extracted zip's folder",
        module=flatten,
    )
    _add_merge_style_subcommand(
        subparsers,
        "merge-photoinfo",
        "Merge Flickr's separately-exported JSON metadata into the flattened directory.",
        default_source_glob="*part*",
        source_glob_help="Glob (relative to source_base) matching each PhotoInformation part folder",
        module=merge_photoinfo,
    )
    _add_dest_plan_subcommand(
        subparsers,
        "rename-plan",
        "Compute the old-name -> <id>.<ext> rename mapping for a flattened directory.",
        dest_help="Flattened directory to compute the plan for",
        module=rename_plan,
    )
    _add_dest_plan_subcommand(
        subparsers,
        "rename",
        "Execute a rename plan written by rename-plan.",
        dest_help="Flattened directory to rename in place",
        module=rename,
    )
    _add_gps_fix(subparsers)
    _add_exif_batches(subparsers)
    _add_exif_write(subparsers)
    _add_cleanup(subparsers)
    # Optional, macOS-only tail: importing the restored dest into Apple
    # Photos. Doesn't exist on Windows/Linux (Photos.app + AppleScript
    # automation are macOS-only) -- see each module's docstring.
    _add_photos_diff(subparsers)
    _add_photos_import(subparsers)
    _add_photos_retry(subparsers)
    _add_photos_verify(subparsers)
    _add_photos_fix_dates(subparsers)
    _add_doctor(subparsers)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "doctor":
        # Only exif-write takes --exiftool-bin; every other subcommand falls
        # back to the default, same binary doctor.py itself defaults to.
        exiftool_bin = getattr(args, "exiftool_bin", exif_write.DEFAULT_EXIFTOOL_BIN)
        print(doctor.render_banner(doctor.check(exiftool_bin)))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
