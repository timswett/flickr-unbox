"""
photos-import / photos-retry -- import a list of files into Apple Photos
via `osxphotos import`, and retry individual stragglers afterward.

Ports `import_batch.sh` + `retry_stragglers.sh` (combined into one module
since they share command-building/report-flag logic -- same reasoning as
`flatten.py`/`merge_photoinfo.py` sharing `_collision_merge.py`). The
gotchas below are load-bearing, not decorative -- each maps directly to a
design choice in the code. Full incident history is in
`Apple_Flickr_PhotosMatch.md` Sessions 4a-4j (flickrmove repo) if you need
the "how this was found" story; this is just the operative facts.

1. Chunked, not one giant call: Apple Photos' AppleScript-driven import
   can hang mid-run, and when it does, osxphotos's own recovery (killall
   + retry) sometimes fails and crashes the whole `osxphotos import`
   process -- losing every file in that chunk not yet processed. `run()`
   loops chunks *internally* within a single call rather than requiring
   the caller to invoke once per chunk; smaller chunks (default 250) mean
   a smaller blast radius per crash and faster detection.

2. `--stop-on-error N` does NOT protect against that crash -- it only
   counts per-file errors osxphotos catches internally. An uncaught
   AppleScript exception kills the whole chunk regardless of this
   threshold.

3. The screen must stay unlocked and the display must stay on for the
   entire run -- Photos' AppleScript automation stalls hard on display
   sleep/lock. Before a long run: System Settings -> Lock Screen -> both
   display-off and password-after-screensaver set to Never; don't
   manually lock during the run. `caffeinate -d -i -u` is a good second
   line of defense against idle sleep specifically.

4. `rc=0` for a chunk does NOT guarantee every file in it succeeded --
   osxphotos can hit its internal error threshold, auto-restart Photos,
   and continue in the same process (still exits 0) while silently
   losing files around the restart point. Neither `run()` nor `retry()`
   parse osxphotos's own output to determine success/failure --
   `verify_photos_import.run()`, diffing the log against the intended
   list afterward, is the only trustworthy completeness signal. Always
   run it after `photos-import`/`photos-retry`.

5. Apple Photos silently drops non-representative files in "burst
   groups" during import (files sharing an embedded Apple burst-UUID
   tag -- can happen even for old individually-uploaded photos that
   originated as an iPhone burst). Only one file per group survives; the
   rest get zero log output. No osxphotos flag disables this. `retry()`
   exists to re-attempt stragglers *one file at a time* -- batching
   former burst-siblings together re-triggers the same bug against each
   other.

6. GIF/PNG: Apple Photos' native import doesn't read embedded EXIF dates
   for these formats, silently falling back to file-modification-time.
   `--exiftool` doesn't fix this (it only sets title/description/
   keywords/location, never date) -- run `fix_media_dates.run()` against
   the target album afterward.
"""
from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import IO, Callable, Iterator, List, Sequence

from . import _osxphotos
from ._preflight import PreflightResult
from ._report import RunSummary

DEFAULT_CHUNK_SIZE = 250
DEFAULT_STOP_ON_ERROR = 20
DEFAULT_REPORT_NAME = "photos_import_report.csv"
DEFAULT_RETRY_REPORT_NAME = "photos_retry_report.csv"
DEFAULT_LOG_NAME = "photos_import.log"

# (argv, cwd, log_fh) -> return code. The subprocess's combined stdout+stderr
# is streamed live into log_fh as it runs (so `tail -f` on that file works
# during an hours-long run, matching exif_write.py's live-streaming design
# decision for the same reason) -- unlike exif_write.py, the captured text
# itself is never parsed here (see lesson 4 above), so only the return code
# is returned.
ProcessRunner = Callable[[List[str], Path, "IO[str]"], int]
WhichFn = _osxphotos.WhichFn
PlatformFn = _osxphotos.PlatformFn

DUP_MODES = ("skip-dups", "allow-duplicates")


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_process_runner(cmd: List[str], cwd: Path, log_fh: "IO[str]") -> int:
    import subprocess

    # List-form argv, no shell=True; cmd is built from a fixed binary name
    # (a CLI flag/default) plus filenames already validated to exist in
    # dest by preflight() -- never raw shell content.
    proc = subprocess.Popen(  # nosec B603
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    if proc.stdout is None:
        raise RuntimeError("subprocess.Popen with stdout=PIPE unexpectedly produced no stdout pipe")
    for line in proc.stdout:
        log_fh.write(line)
        log_fh.flush()
    proc.wait()
    return proc.returncode


def load_files_list(path: Path) -> List[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def _chunked(items: Sequence[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


# --- photos-import: chunked batch ---


def build_import_command(
    osxphotos_bin: str,
    chunk_files: Sequence[str],
    album: str,
    stop_on_error: int,
    report_path: Path,
    append: bool,
) -> List[str]:
    cmd = [
        osxphotos_bin, "import", *chunk_files,
        "--album", album,
        "--skip-dups",
        "--exiftool",
        "-V", "--no-progress",
        "--stop-on-error", str(stop_on_error),
        # .resolve(): the subprocess runs with cwd=dest (so bare chunk_files
        # basenames resolve correctly), so report_path itself must not
        # depend on that same cwd -- same reasoning as exif_write.py's
        # build_command() resolving its batch-file path.
        "-o", str(report_path.resolve()),
    ]
    if append:
        cmd.append("-O")
    return cmd


def preflight(
    dest: Path,
    files_list_path: Path,
    osxphotos_bin: str,
    which_fn: WhichFn,
    platform_fn: PlatformFn,
) -> PreflightResult:
    if not dest.is_dir():
        return PreflightResult.failed(f"dest does not exist or is not a directory: {dest}")
    if not files_list_path.is_file():
        return PreflightResult.failed(f"files list not found: {files_list_path}")

    reasons = []
    platform_result = _osxphotos.preflight_platform_and_binary(osxphotos_bin, which_fn, platform_fn)
    if not platform_result.ok:
        reasons.extend(platform_result.reasons)

    filenames = load_files_list(files_list_path)
    missing = [name for name in filenames if not (dest / name).exists()]
    if missing:
        preview = missing[:5]
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        reasons.append(
            f"{len(missing)} file(s) listed in the files list are missing from dest: {preview}{more}"
        )

    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()


def run(
    dest: Path,
    files_list_path: Path,
    album: str,
    dry_run: bool,
    *,
    batch_dir: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    stop_on_error: int = DEFAULT_STOP_ON_ERROR,
    osxphotos_bin: str = "osxphotos",
    report_name: str = DEFAULT_REPORT_NAME,
    log_name: str = DEFAULT_LOG_NAME,
    process_runner: ProcessRunner = _default_process_runner,
    which_fn: WhichFn = shutil.which,
    platform_fn: PlatformFn = lambda: sys.platform,
) -> RunSummary:
    summary = RunSummary(stage="photos-import", dry_run=dry_run)

    result = preflight(dest, files_list_path, osxphotos_bin, which_fn, platform_fn)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    filenames = load_files_list(files_list_path)
    chunks = list(_chunked(filenames, chunk_size))
    summary.bump("total_files", len(filenames))
    summary.bump("chunks", len(chunks))

    report_path = batch_dir / report_name
    if dry_run:
        if chunks:
            sample_cmd = build_import_command(osxphotos_bin, chunks[0], album, stop_on_error, report_path, append=False)
            summary.note(f"first chunk command: {' '.join(sample_cmd)}")
        summary.note("osxphotos not invoked (dry run)")
        return summary

    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / log_name

    processed = 0
    chunk_failures = 0
    with open(log_path, "a") as log_fh:
        log_fh.write(f"{_timestamp()} START batch={files_list_path} total={len(filenames)} album={album!r}\n")
        log_fh.flush()
        for i, chunk in enumerate(chunks, start=1):
            start = time.monotonic()
            # append=(i > 1): matches import_batch.sh's own first_report
            # logic exactly -- a fresh run() call always starts its report
            # in write mode for chunk 1, even if report_path already has
            # content from an earlier call. Preserved as-is (not "fixed")
            # since production runs always relied on this exact behavior.
            cmd = build_import_command(osxphotos_bin, chunk, album, stop_on_error, report_path, append=(i > 1))
            rc = process_runner(cmd, dest, log_fh)
            elapsed = int(time.monotonic() - start)
            processed += len(chunk)
            log_fh.write(
                f"{_timestamp()} chunk={i} files_in_chunk={len(chunk)} "
                f"processed={processed}/{len(filenames)} rc={rc} elapsed={elapsed}s\n"
            )
            log_fh.flush()
            if rc != 0:
                chunk_failures += 1
                summary.note(
                    f"chunk {i} exited rc={rc} (may include the --stop-on-error threshold, "
                    f"or a real crash -- check {log_path} around this point); continuing to next chunk"
                )
        log_fh.write(
            f"{_timestamp()} DONE batch={files_list_path} processed={processed}/{len(filenames)} "
            "-- run photos-verify before trusting this\n"
        )

    summary.bump("chunks_run", len(chunks))
    summary.bump("chunk_failures", chunk_failures)
    summary.note(f"log: {log_path}")
    summary.note(f"report: {report_path}")
    summary.note("rc=0 does not guarantee every file succeeded -- run photos-verify next, don't trust this alone")

    return summary


# --- photos-retry: one file at a time ---


def build_retry_command(
    osxphotos_bin: str, path: Path, album: str, mode: str, report_path: Path, append: bool
) -> List[str]:
    cmd = [osxphotos_bin, "import", str(path), "--album", album]
    if mode == "skip-dups":
        cmd.append("--skip-dups")
    # .resolve(): same cwd-mismatch reasoning as build_import_command above.
    cmd += ["--exiftool", "-V", "--no-progress", "-o", str(report_path.resolve())]
    if append:
        cmd.append("-O")
    return cmd


def retry_preflight(
    dest: Path,
    basenames_file: Path,
    mode: str,
    osxphotos_bin: str,
    which_fn: WhichFn,
    platform_fn: PlatformFn,
) -> PreflightResult:
    reasons = []
    if not dest.is_dir():
        reasons.append(f"dest does not exist or is not a directory: {dest}")
    if not basenames_file.is_file():
        reasons.append(f"basenames file not found: {basenames_file}")
    if mode not in DUP_MODES:
        reasons.append(f"mode must be one of {DUP_MODES}, got: {mode!r}")

    platform_result = _osxphotos.preflight_platform_and_binary(osxphotos_bin, which_fn, platform_fn)
    if not platform_result.ok:
        reasons.extend(platform_result.reasons)

    # Same existence check as run()'s preflight() -- basenames_file is often
    # a stale or hand-edited errors.txt/missing.txt from photos-verify; fail
    # clearly up front rather than letting osxphotos hit it one file at a
    # time deep into the run.
    if dest.is_dir() and basenames_file.is_file():
        basenames = load_files_list(basenames_file)
        missing = [name for name in basenames if not (dest / name).exists()]
        if missing:
            preview = missing[:5]
            more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
            reasons.append(
                f"{len(missing)} file(s) listed in the basenames file are missing from dest: {preview}{more}"
            )

    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()


def retry(
    dest: Path,
    basenames_file: Path,
    album: str,
    mode: str,
    dry_run: bool,
    *,
    batch_dir: Path,
    osxphotos_bin: str = "osxphotos",
    report_name: str = DEFAULT_RETRY_REPORT_NAME,
    log_name: str = DEFAULT_LOG_NAME,
    process_runner: ProcessRunner = _default_process_runner,
    which_fn: WhichFn = shutil.which,
    platform_fn: PlatformFn = lambda: sys.platform,
) -> RunSummary:
    summary = RunSummary(stage="photos-retry", dry_run=dry_run)

    result = retry_preflight(dest, basenames_file, mode, osxphotos_bin, which_fn, platform_fn)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    basenames = load_files_list(basenames_file)
    summary.bump("total_files", len(basenames))

    report_path = batch_dir / report_name
    if dry_run:
        if basenames:
            sample_cmd = build_retry_command(osxphotos_bin, dest / basenames[0], album, mode, report_path, append=False)
            summary.note(f"first file command: {' '.join(sample_cmd)}")
        summary.note("osxphotos not invoked (dry run)")
        return summary

    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / log_name

    n = 0
    with open(log_path, "a") as log_fh:
        log_fh.write(f"{_timestamp()} START retry-stragglers total={len(basenames)} mode={mode} album={album!r}\n")
        log_fh.flush()
        for fname in basenames:
            n += 1
            path = dest / fname
            cmd = build_retry_command(osxphotos_bin, path, album, mode, report_path, append=(n > 1))
            rc = process_runner(cmd, dest, log_fh)
            log_fh.write(f"{_timestamp()} retry-file={n}/{len(basenames)} file={fname} rc={rc}\n")
            log_fh.flush()
        log_fh.write(
            f"{_timestamp()} DONE retry-stragglers n={n} -- re-run photos-verify against "
            "the full intended list to confirm\n"
        )

    summary.bump("files_retried", n)
    summary.note(f"log: {log_path}")
    summary.note(f"report: {report_path}")

    return summary
