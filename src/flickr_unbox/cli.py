"""
flickr-unbox -- single CLI, one subcommand per pipeline stage.

Every subcommand defaults to a dry run (full validation + summary, nothing
on disk changes) and requires --no-dry-run to act for real; --no-dry-run
re-validates from current on-disk state rather than trusting an earlier
dry-run. See FLICKR_UNBOX_HANDOFF.md "Python port design" for the full
reasoning -- this file is deliberately thin, one block per stage, so that
doc stays the source of truth for *why* rather than this file growing its
own competing explanation.

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

from . import cleanup, doctor, exif_batches, exif_write, flatten, gps_fix, merge_photoinfo, rename, rename_plan


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
    p.set_defaults(func=_run_doctor)


def _run_doctor(args: argparse.Namespace) -> int:
    summary = doctor.run(args.exiftool_bin)
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
    _add_doctor(subparsers)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "doctor":
        print(doctor.render_banner(doctor.check()))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
