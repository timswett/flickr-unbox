"""
exif-write -- run the validated exiftool command against one batch of files.

Purpose:
    Writes Title/Description/DateTimeOriginal/GPS/Keywords from each
    `<id>.json` sidecar into its matching `<id>.<ext>` photo/video, via
    exiftool's `-tagsfromfile`. The exact tag mapping below was validated
    against 5 diverse real samples (GPS+tags, album, minimal/private,
    video, title-slug-renamed) before ever touching real data -- see
    CLAUDE_CODE_HANDOFF.md. Operates one batch at a time (matching
    `exif_write_batch.sh <NN>`'s real operational workflow: run a batch,
    review it, `cleanup` it to free space, then move to the next) rather
    than a "do everything" mode, since the per-batch human checkpoint is
    what let a real problem (batch 03's 2 corrupt-EXIF PNGs) get caught
    before it could spread.

Live-streamed output (design decision, not a mechanical port choice):
    Bash's script doesn't capture exiftool's output -- it streams straight
    through, and the real migration's own operating instructions
    (CLAUDE_CODE_HANDOFF.md) rely on that: `nohup ... > log.txt 2>&1 &`
    then `tail -f log.txt` to watch a 50+ minute run live. A naive
    `subprocess.run(capture_output=True)` port would silently break that --
    output would only appear after the whole batch finished. Instead,
    `_default_process_runner` streams via `Popen`, printing each line
    immediately (so redirection/`tail -f` still works exactly like bash)
    while also buffering the full text for the parsing pass below.

TODO #4 -- parsed summary instead of manual log-grep:
    Regexes below were built directly from real captured output in
    exif_batch_01.log through exif_batch_08.log, not guessed:
        " 2822 image files updated"
        "    2 files weren't updated due to errors"
        "Error: [minor] IFD0 pointer references previous IFD0 directory - 50531875228.png"
        "Warning: [Minor] Duplicate XMP property: ... - 47004786621.jpg"
    All 8 real batches (mixing .jpg/.png/.mov) only ever produced "image
    files updated" -- never a separate "video files updated" -- so one
    regex set covers every media type actually seen. Every `Error:` line
    is logged individually in the summary (few, actionable);
    `Warning:` lines are only counted (batch 01 alone had 868 of them --
    logging each would bury the signal), matching the "benign, don't need
    individual scrutiny" call already made in CLAUDE_CODE_HANDOFF.md.
    Deliberately NOT adding `-m` (ignore minor errors) -- preserves that
    same document's reasoning: `-m` would silently downgrade every minor-
    error class, not just the one already vetted (the IFD0/PNG case).

TODO #5 -- portable free-space check:
    `shutil.disk_usage(dest).free` replaces bash's BSD-only `stat -f`/
    `df -k`. Same 1.2x safety margin. Batch size is computed by re-
    `stat()`-ing each listed file fresh, not trusting `exif-batches`'
    plan-time sizes -- matches bash's own re-stat behavior.

New pre-flight check (not in bash): every filename listed in the batch
    file must still exist in `dest`, or the whole batch refuses to start
    (a missing file suggests the batch plan is stale -- re-run
    `exif-batches`). Also checks the `exiftool` binary is actually on
    PATH before doing anything, with a friendlier message than a raw
    subprocess FileNotFoundError.

New artifact -- the result receipt (needed for `cleanup`'s safety gate):
    `cleanup.py` is speced to refuse running unless its batch's
    `exif-write` reported zero errors -- but those are two separate CLI
    invocations, possibly hours apart. For that gate to be enforceable,
    this stage writes `batch_dir/batch_{NN}.result.json` after every REAL
    run (never on a dry run -- a fake "0 errors" receipt from a dry run,
    where nothing was actually written, would be actively dangerous for
    `cleanup` to trust). Lives in `batch_dir` next to `batch_NN.txt`,
    keeping `dest` itself free of pipeline bookkeeping.

Testability: the free-space check, the `exiftool`-on-PATH check, and the
    exiftool invocation itself are all injectable (`disk_usage_fn`,
    `which_fn`, `process_runner`), so the fast synthetic test suite can
    exercise the space-check-abort path and the output-parsing logic with
    canned/fake values -- without touching a real disk or needing
    `exiftool` installed. Validating against a real `exiftool` binary and
    real files stays a separate ad hoc pass, same pattern as every other
    stage in this port, not part of the committed suite.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import _collision_merge
from ._preflight import PreflightResult
from ._report import RunSummary

DEFAULT_EXIFTOOL_BIN = "exiftool"

# Validated against 5 diverse real samples before ever touching real data --
# see CLAUDE_CODE_HANDOFF.md. The "*" wildcard on GPSLatitude*/GPSLongitude*
# is required: without it, N/S/E/W hemisphere refs don't get set correctly.
EXIFTOOL_TAG_ARGS = [
    "-tagsfromfile", "%d%f.json",
    "-Title<Name",
    "-Description<Description",
    "-DateTimeOriginal<Date_taken",
    "-CreateDate<Date_taken",
    "-DateTime<Date_taken",
    "-GPSLatitude*<GeoLatitude",
    "-GPSLongitude*<GeoLongitude",
    "-Keywords<TagsTag",
    "-Subject<TagsTag",
]

# Broadened from an exact-text match on "image files updated": all 8 real
# batches this was originally built from (exif_batch_01.log..08.log, mixing
# .jpg/.png/.mov) only ever produced that one exact line, but exiftool's own
# vocabulary is wider than that single sample -- e.g. "N files unchanged"
# for files with nothing to write, or plausibly "N video files updated" for
# a batch that happens to be all-video (categorized by type). Summing every
# matching line instead of expecting exactly one avoids silently under-
# counting (and tripping cleanup's stale-receipt check) if a real batch ever
# produces a variant this project's own logs never happened to include.
_UPDATED_RE = re.compile(r"^\s*(\d+) \w+ files updated\s*$", re.MULTILINE)
_UNCHANGED_RE = re.compile(r"^\s*(\d+) \w+ files unchanged\s*$", re.MULTILINE)
_ERRORED_RE = re.compile(r"^\s*(\d+) files weren't updated due to errors\s*$", re.MULTILINE)
_ERROR_LINE_RE = re.compile(r"^Error: (.+) - (\S+)\s*$", re.MULTILINE)
_WARNING_LINE_RE = re.compile(r"^Warning: (.+) - (\S+)\s*$", re.MULTILINE)

# (combined stdout+stderr text, process return code)
ProcessRunner = Callable[[List[str], Path], "Tuple[str, int]"]
DiskUsageFn = Callable[[Path], "shutil._ntuple_diskusage"]  # noqa: F821 -- typing-only reference
WhichFn = Callable[[str], Optional[str]]


@dataclass
class ExifWriteResult:
    files_updated: int = 0
    files_unchanged: int = 0
    files_errored: int = 0
    warnings: int = 0
    error_lines: List[Tuple[str, str]] = field(default_factory=list)  # (message, filename)


def load_batch_file(batch_path: Path) -> List[str]:
    with open(batch_path) as f:
        return [line.strip() for line in f if line.strip()]


def batch_size_bytes(dest: Path, filenames: List[str]) -> int:
    total = 0
    for name in filenames:
        p = dest / name
        if p.exists():
            total += p.stat().st_size
    return total


def check_free_space(
    dest: Path,
    batch_bytes: int,
    safety_factor: float = 1.2,
    disk_usage_fn: DiskUsageFn = shutil.disk_usage,
) -> PreflightResult:
    avail = disk_usage_fn(dest).free
    required = int(batch_bytes * safety_factor)
    if avail < required:
        return PreflightResult.failed(
            f"not enough free space: need ~{required / 1024**3:.2f} GB "
            f"({safety_factor}x batch size for a safety margin) but only "
            f"{avail / 1024**3:.2f} GB available -- clean up _original backups "
            "from previously verified batches before continuing"
        )
    return PreflightResult.passed()


# The one source key EXIFTOOL_TAG_ARGS relies on that's a literal top-level
# JSON key (not one synthesized by exiftool from a nested structure, like
# GeoLatitude/TagsTag are) -- present in every real sidecar validated so
# far, even when its value is empty. Used as a cheap schema-sanity check,
# not because Title is more important than the other fields.
_SCHEMA_CHECK_KEY = "Name"
_SCHEMA_CHECK_SAMPLE_SIZE = 5


def check_sidecar_schema(
    dest: Path, filenames: List[str], sample_size: int = _SCHEMA_CHECK_SAMPLE_SIZE
) -> PreflightResult:
    """
    Cheap sanity check that this batch's JSON sidecars actually look like
    the schema EXIFTOOL_TAG_ARGS was built for, before spending real time
    invoking exiftool. `-tagsfromfile` silently no-ops on a source field
    that doesn't exist in the sidecar -- it isn't an error -- so a schema
    mismatch (a different Flickr export era/tool using different JSON key
    names, plausible for other forks/users of this generalized tool) would
    otherwise run cleanly with 0 reported errors while writing nothing at
    all. Samples rather than checking every sidecar (batches can be
    thousands of files): one structurally different sidecar among many is
    normal, a batch where NONE of the sample has the most basic expected
    key is not.
    """
    checked = 0
    for name in filenames[:sample_size]:
        sidecar = dest / f"{Path(name).stem}.json"
        if not sidecar.is_file():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        checked += 1
        # Case-insensitive: exiftool's own -tagsfromfile matching is
        # case-insensitive (confirmed against real sidecars, which use
        # lowercase "name" despite EXIFTOOL_TAG_ARGS referencing "Name"),
        # so the check needs to match that or it flags real, working data.
        if isinstance(data, dict) and any(k.lower() == _SCHEMA_CHECK_KEY.lower() for k in data):
            return PreflightResult.passed()

    if checked == 0:
        return PreflightResult.passed()  # nothing readable to sample -- don't block on this alone

    return PreflightResult.failed(
        f"none of the {checked} sampled JSON sidecar(s) have a {_SCHEMA_CHECK_KEY!r} key -- "
        "this looks like a different Flickr export schema than EXIFTOOL_TAG_ARGS was built "
        "for (-tagsfromfile silently writes nothing for a missing source field rather than "
        "erroring), so proceeding would likely report 0 errors while writing 0 real fields"
    )


def preflight(
    dest: Path,
    batch_path: Path,
    exiftool_bin: str,
    disk_usage_fn: DiskUsageFn = shutil.disk_usage,
    which_fn: WhichFn = shutil.which,
) -> PreflightResult:
    if not batch_path.is_file():
        return PreflightResult.failed(f"batch file not found: {batch_path}")
    if not dest.is_dir():
        return PreflightResult.failed(f"dest does not exist or is not a directory: {dest}")

    reasons = []
    if which_fn(exiftool_bin) is None:
        reasons.append(
            f"exiftool binary not found on PATH: {exiftool_bin!r} -- install it from https://exiftool.org/"
        )

    filenames = load_batch_file(batch_path)
    missing = [name for name in filenames if not (dest / name).exists()]
    if missing:
        preview = missing[:5]
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        reasons.append(
            f"{len(missing)} file(s) listed in the batch are missing from dest -- "
            f"batch plan may be stale, re-run exif-batches: {preview}{more}"
        )

    if not reasons:  # only trust these checks if the file list itself was sound
        schema_result = check_sidecar_schema(dest, filenames)
        if not schema_result.ok:
            reasons.extend(schema_result.reasons)

        space_result = check_free_space(
            dest, batch_size_bytes(dest, filenames), disk_usage_fn=disk_usage_fn
        )
        if not space_result.ok:
            reasons.extend(space_result.reasons)

    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()


def build_command(exiftool_bin: str, batch_path: Path) -> List[str]:
    # batch_path is resolved to absolute: the subprocess's cwd is set to dest
    # (so bare filenames inside the batch file resolve correctly), and the
    # list-file argument itself must not depend on that same cwd.
    return [exiftool_bin, *EXIFTOOL_TAG_ARGS, "-progress", "-@", str(batch_path.resolve())]


def _default_process_runner(cmd: List[str], cwd: Path) -> Tuple[str, int]:
    """Run cmd, streaming its combined stdout+stderr live (so redirecting to a
    log file and `tail -f`-ing it still works exactly like the bash version),
    while also returning the full captured text and exit code for the
    caller to interpret."""
    lines: List[str] = []
    # List-form argv (see build_command()), no shell=True.
    proc = subprocess.Popen(  # nosec B603
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    # An explicit check, not `assert`: stdout=PIPE above guarantees this never
    # fires, but `assert` is stripped under `python -O` -- a bare `for line in
    # None` would then raise a confusing TypeError instead of failing clearly.
    if proc.stdout is None:
        raise RuntimeError("subprocess.Popen with stdout=PIPE unexpectedly produced no stdout pipe")
    for line in proc.stdout:
        print(line, end="")
        lines.append(line)
    proc.wait()
    return "".join(lines), proc.returncode


def parse_output(text: str) -> ExifWriteResult:
    result = ExifWriteResult()
    # Summed rather than a single .search(): exiftool can emit more than one
    # "N <word> files updated/unchanged" line in principle (e.g. separate
    # image/video categories) -- see the regex comments above.
    result.files_updated = sum(int(n) for n in _UPDATED_RE.findall(text))
    result.files_unchanged = sum(int(n) for n in _UNCHANGED_RE.findall(text))
    m = _ERRORED_RE.search(text)
    if m:
        result.files_errored = int(m.group(1))
    result.error_lines = _ERROR_LINE_RE.findall(text)
    result.warnings = len(_WARNING_LINE_RE.findall(text))
    return result


def write_result_receipt(batch_dir: Path, batch_num: str, result: ExifWriteResult) -> Path:
    receipt_path = batch_dir / f"batch_{batch_num}.result.json"
    payload = {
        "batch_num": batch_num,
        "files_updated": result.files_updated,
        "files_unchanged": result.files_unchanged,
        "files_errored": result.files_errored,
        "warnings": result.warnings,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(receipt_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return receipt_path


def _find_top_level_junk(dest: Path) -> List[Path]:
    return sorted(p for p in dest.glob("*") if p.is_file() and _collision_merge.is_junk(p))


def run(
    dest: Path,
    batch_dir: Path,
    batch_num: str,
    dry_run: bool,
    exiftool_bin: str = DEFAULT_EXIFTOOL_BIN,
    disk_usage_fn: DiskUsageFn = shutil.disk_usage,
    which_fn: WhichFn = shutil.which,
    process_runner: ProcessRunner = _default_process_runner,
) -> RunSummary:
    summary = RunSummary(stage="exif-write", dry_run=dry_run)
    batch_path = batch_dir / f"batch_{batch_num}.txt"

    result = preflight(dest, batch_path, exiftool_bin, disk_usage_fn=disk_usage_fn, which_fn=which_fn)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    junk = _find_top_level_junk(dest)
    summary.bump("junk_files_found", len(junk))
    if not dry_run:
        for j in junk:
            j.unlink()

    filenames = load_batch_file(batch_path)
    batch_bytes = batch_size_bytes(dest, filenames)
    summary.bump("batch_files", len(filenames))
    summary.note(f"batch size: {batch_bytes / 1024**3:.2f} GB")

    if dry_run:
        avail = disk_usage_fn(dest).free
        summary.note(
            f"available space: {avail / 1024**3:.2f} GB "
            f"(would need ~{batch_bytes * 1.2 / 1024**3:.2f} GB)"
        )
        summary.note("exiftool not invoked (dry run)")
        return summary

    cmd = build_command(exiftool_bin, batch_path)
    output_text, returncode = process_runner(cmd, dest)

    if returncode != 0:
        # A crashed/killed exiftool process (OOM, disk full, SIGTERM) may
        # print no "files updated"/"files weren't updated" line at all --
        # parse_output() would then report 0 errors, which could satisfy
        # cleanup's safety gate despite the batch never actually being
        # written. Treat a nonzero exit as a hard failure and don't write a
        # receipt at all, same as if exif-write had never run for real.
        summary.bump("process_failed")
        summary.note(
            f"exiftool exited with code {returncode} -- treating this as a hard failure. "
            "No result receipt was written; check the full output above for what happened "
            "before re-running."
        )
        return summary

    parsed = parse_output(output_text)

    summary.bump("files_updated", parsed.files_updated)
    summary.bump("files_unchanged", parsed.files_unchanged)
    summary.bump("files_errored", parsed.files_errored)
    summary.bump("warnings", parsed.warnings)
    for message, filename in parsed.error_lines:
        summary.note(f"ERROR: {filename}: {message}")

    accounted_for = parsed.files_updated + parsed.files_unchanged + parsed.files_errored
    if accounted_for != len(filenames):
        summary.note(
            f"NOTE: batch had {len(filenames)} file(s) but exiftool's summary only "
            f"accounts for {accounted_for} ({parsed.files_updated} updated + "
            f"{parsed.files_unchanged} unchanged + {parsed.files_errored} errored) -- "
            "worth a manual look at the full output"
        )

    receipt_path = write_result_receipt(batch_dir, batch_num, parsed)
    summary.note(f"result receipt written to {receipt_path}")

    return summary
