"""
cleanup -- delete a batch's exiftool `_original` backup files once
exif-write has been verified clean.

Purpose:
    exiftool's default behavior creates a `<file>_original` backup next to
    every file it writes to. Once a batch has been written and verified,
    those backups are pure disk-space overhead -- this stage removes them
    so the next batch's `exif-write` has room to run (see
    exif_batches.py's docstring for why batching is needed at all).

New safety gate (not in cleanup_batch_originals.sh):
    Bash's version deletes on request, no questions asked -- it's on the
    operator to remember to check the exif-write log for errors first.
    This port refuses by default unless exif_write.py's
    `batch_dir/batch_{NN}.result.json` receipt says the batch had zero
    errors, with three distinct refusal reasons (not one generic "gate
    failed" note, since they mean different things to the operator):
      - receipt missing entirely -- exif-write was never run for real on
        this batch (or only ever dry-run)
      - receipt shows files_errored > 0 -- the actual case the gate
        exists for
      - receipt looks stale -- files_updated + files_errored in the
        receipt doesn't match the batch file's current line count,
        meaning exif-batches was likely re-run (re-planning the batch)
        after exif-write last ran, so the receipt no longer describes
        what's actually in the batch file now -- same philosophy as
        rename.py's stale-plan check.
    All three run in both dry-run and --no-dry-run, like every other
    pre-flight check in this design: a dry run tells you up front whether
    cleanup would be refused, not just when you try it for real.

    --force bypasses only these three receipt-gate conditions, never the
    structural checks (batch file missing, dest missing) -- for the
    legitimate edge case where the receipt was lost but the operator
    otherwise confirmed the batch was clean.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ._preflight import PreflightResult
from ._report import RunSummary


def load_batch_file(batch_path: Path) -> List[str]:
    with open(batch_path) as f:
        return [line.strip() for line in f if line.strip()]


def load_receipt(batch_dir: Path, batch_num: str) -> Optional[dict]:
    """Returns the parsed result receipt, or None if missing/unreadable."""
    receipt_path = batch_dir / f"batch_{batch_num}.result.json"
    if not receipt_path.is_file():
        return None
    try:
        return json.loads(receipt_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# Required, not defaulted: a receipt missing one of these looks malformed
# or from an incompatible version, and silently defaulting it to 0 would
# let a broken receipt pass the safety gate this check exists to enforce.
_REQUIRED_RECEIPT_KEYS = ("batch_num", "files_updated", "files_errored")


def check_receipt_gate(batch_dir: Path, batch_num: str, batch_file_count: int) -> PreflightResult:
    receipt_path = batch_dir / f"batch_{batch_num}.result.json"
    receipt = load_receipt(batch_dir, batch_num)

    if receipt is None:
        if receipt_path.is_file():
            return PreflightResult.failed(f"result receipt at {receipt_path} could not be parsed as JSON")
        return PreflightResult.failed(
            f"no result receipt found at {receipt_path} -- exif-write was never run for "
            "real on this batch (or only ever dry-run); run it before cleanup"
        )

    missing_keys = [k for k in _REQUIRED_RECEIPT_KEYS if k not in receipt]
    if missing_keys:
        return PreflightResult.failed(
            f"result receipt at {receipt_path} is missing required field(s) {missing_keys} -- "
            "looks malformed or from an incompatible flickr-unbox version; re-run exif-write"
        )

    # The receipt carries its own batch_num specifically so this can be
    # checked -- catches a receipt copied/misplaced from a different batch
    # (or a future bug) that would otherwise only be caught if the file
    # count also happened to mismatch.
    if str(receipt["batch_num"]) != str(batch_num):
        return PreflightResult.failed(
            f"result receipt at {receipt_path} is for batch {receipt['batch_num']!r}, not "
            f"batch {batch_num!r} -- it looks like it was copied or misplaced from a "
            "different batch; re-run exif-write for this batch"
        )

    files_errored = receipt["files_errored"]
    if files_errored:
        return PreflightResult.failed(
            f"exif-write reported {files_errored} error(s) for this batch (see {receipt_path}) -- "
            "resolve them before cleanup"
        )

    # files_unchanged is optional -- older receipts (written before this
    # field existed) won't have it, and default to 0 for them.
    accounted_for = receipt["files_updated"] + receipt.get("files_unchanged", 0) + files_errored
    if accounted_for != batch_file_count:
        return PreflightResult.failed(
            f"result receipt looks stale: it accounts for {accounted_for} file(s) but the "
            f"batch file currently lists {batch_file_count} -- exif-batches may have been "
            "re-run since exif-write last ran; re-run exif-write before cleanup"
        )

    return PreflightResult.passed()


def preflight(
    dest: Path, batch_path: Path, batch_dir: Path, batch_num: str, force: bool
) -> PreflightResult:
    if not batch_path.is_file():
        return PreflightResult.failed(f"batch file not found: {batch_path}")
    if not dest.is_dir():
        return PreflightResult.failed(f"dest does not exist or is not a directory: {dest}")

    if force:
        return PreflightResult.passed()

    batch_file_count = len(load_batch_file(batch_path))
    return check_receipt_gate(batch_dir, batch_num, batch_file_count)


def run(dest: Path, batch_dir: Path, batch_num: str, dry_run: bool, force: bool = False) -> RunSummary:
    summary = RunSummary(stage="cleanup", dry_run=dry_run)
    batch_path = batch_dir / f"batch_{batch_num}.txt"

    result = preflight(dest, batch_path, batch_dir, batch_num, force)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    if force:
        summary.note("--force: skipped the result-receipt safety gate")

    removed = 0
    missing = 0
    failed = 0
    for name in load_batch_file(batch_path):
        original = dest / f"{name}_original"
        if original.is_file():
            if not dry_run:
                try:
                    original.unlink()
                except OSError as e:
                    failed += 1
                    summary.note(f"FAILED: {original}: {e}")
                    continue
            removed += 1
        else:
            missing += 1

    summary.bump("removed" if not dry_run else "would_remove", removed)
    summary.bump("already_clean_or_never_created", missing)
    if failed:
        summary.bump("failed", failed)

    return summary
