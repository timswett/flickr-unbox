"""
Tests for flickr_unbox.photos_import (both run() -- chunked batch import --
and retry() -- one-file-at-a-time straggler retry).

No real osxphotos binary or real Photos.app needed: the subprocess call is
injectable (process_runner takes (cmd, cwd, log_fh) -> returncode, per
photos_import.py's ProcessRunner type), so every test drives canned return
codes and inspects the resulting log file / RunSummary.
"""
import json
from pathlib import Path

import pytest

from flickr_unbox import photos_import


def _touch(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_files_list(path: Path, names) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(names) + "\n")


_MACOS_KWARGS = dict(which_fn=lambda name: f"/usr/bin/{name}", platform_fn=lambda: "darwin")


# --- chunking ---

def test_chunked_splits_evenly():
    chunks = list(photos_import._chunked(["a", "b", "c", "d"], 2))
    assert chunks == [["a", "b"], ["c", "d"]]


def test_chunked_last_chunk_is_partial():
    chunks = list(photos_import._chunked(["a", "b", "c"], 2))
    assert chunks == [["a", "b"], ["c"]]


def test_chunked_empty_input():
    assert list(photos_import._chunked([], 250)) == []


# --- build_import_command() / build_retry_command() ---

def test_build_import_command_no_append_flag_on_first_chunk():
    cmd = photos_import.build_import_command("osxphotos", ["a.jpg"], "Album", 20, Path("r.csv"), append=False)
    assert "-O" not in cmd
    assert cmd[:3] == ["osxphotos", "import", "a.jpg"]


def test_build_import_command_appends_dash_o_capital_on_later_chunks():
    cmd = photos_import.build_import_command("osxphotos", ["a.jpg"], "Album", 20, Path("r.csv"), append=True)
    assert cmd[-1] == "-O"


def test_build_retry_command_skip_dups_mode_includes_flag():
    cmd = photos_import.build_retry_command("osxphotos", Path("a.jpg"), "Album", "skip-dups", Path("r.csv"), False)
    assert "--skip-dups" in cmd


def test_build_retry_command_allow_duplicates_mode_omits_flag():
    cmd = photos_import.build_retry_command("osxphotos", Path("a.jpg"), "Album", "allow-duplicates", Path("r.csv"), False)
    assert "--skip-dups" not in cmd


def test_build_import_command_resolves_a_relative_report_path(tmp_path, monkeypatch):
    # The subprocess runs with cwd=dest, so a relative report_path must be
    # resolved against the real invocation cwd here, not left to be
    # resolved (wrongly, against dest) by the osxphotos subprocess itself.
    monkeypatch.chdir(tmp_path)
    cmd = photos_import.build_import_command("osxphotos", ["a.jpg"], "Album", 20, Path("batch/r.csv"), append=False)
    o_index = cmd.index("-o")
    assert cmd[o_index + 1] == str(tmp_path / "batch" / "r.csv")


def test_build_retry_command_resolves_a_relative_report_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = photos_import.build_retry_command("osxphotos", Path("a.jpg"), "Album", "skip-dups", Path("batch/r.csv"), False)
    o_index = cmd.index("-o")
    assert cmd[o_index + 1] == str(tmp_path / "batch" / "r.csv")


# --- run(): preflight ---

def test_run_preflight_fails_on_non_macos(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    files_list = tmp_path / "files.txt"
    _write_files_list(files_list, ["a.jpg"])
    summary = photos_import.run(
        dest, files_list, "Album", dry_run=True, batch_dir=tmp_path / "batch",
        which_fn=lambda n: f"/usr/bin/{n}", platform_fn=lambda: "linux",
    )
    assert summary.counts["preflight_failed"] == 1


def test_run_preflight_fails_when_files_list_missing(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    summary = photos_import.run(
        dest, tmp_path / "nope.txt", "Album", dry_run=True, batch_dir=tmp_path / "batch", **_MACOS_KWARGS
    )
    assert summary.counts["preflight_failed"] == 1


def test_run_preflight_fails_when_a_listed_file_is_missing_from_dest(tmp_path):
    dest = tmp_path / "dest"
    _touch(dest / "a.jpg")
    files_list = tmp_path / "files.txt"
    _write_files_list(files_list, ["a.jpg", "gone.jpg"])
    summary = photos_import.run(
        dest, files_list, "Album", dry_run=True, batch_dir=tmp_path / "batch", **_MACOS_KWARGS
    )
    assert summary.counts["preflight_failed"] == 1


# --- run(): dry run ---

def test_run_dry_run_invokes_no_process_and_writes_no_log(tmp_path):
    dest = tmp_path / "dest"
    _touch(dest / "a.jpg")
    files_list = tmp_path / "files.txt"
    _write_files_list(files_list, ["a.jpg"])
    batch_dir = tmp_path / "batch"

    calls = []
    summary = photos_import.run(
        dest, files_list, "Album", dry_run=True, batch_dir=batch_dir,
        process_runner=lambda cmd, cwd, log_fh: calls.append(cmd) or 0,
        **_MACOS_KWARGS,
    )

    assert calls == []
    assert not batch_dir.exists()
    assert summary.counts["total_files"] == 1
    assert summary.counts["chunks"] == 1


# --- run(): real run, chunking + log-and-continue ---

def test_run_splits_into_chunks_and_continues_past_a_failed_chunk(tmp_path):
    dest = tmp_path / "dest"
    names = [f"{i}.jpg" for i in range(5)]
    for n in names:
        _touch(dest / n)
    files_list = tmp_path / "files.txt"
    _write_files_list(files_list, names)
    batch_dir = tmp_path / "batch"

    rcs = iter([1, 0, 0])  # first chunk "fails", the rest succeed
    calls = []

    def fake_runner(cmd, cwd, log_fh):
        calls.append((cmd, cwd))
        return next(rcs)

    summary = photos_import.run(
        dest, files_list, "Album", dry_run=False, batch_dir=batch_dir,
        chunk_size=2, process_runner=fake_runner, **_MACOS_KWARGS,
    )

    assert len(calls) == 3  # 5 files / chunk_size=2 -> 3 chunks, all run despite chunk 1's failure
    assert all(cwd == dest for _cmd, cwd in calls)
    assert summary.counts["chunks_run"] == 3
    assert summary.counts["chunk_failures"] == 1

    log_text = (batch_dir / photos_import.DEFAULT_LOG_NAME).read_text()
    assert "START batch=" in log_text
    assert "chunk=1 files_in_chunk=2 processed=2/5 rc=1" in log_text
    assert "chunk=3 files_in_chunk=1 processed=5/5 rc=0" in log_text
    assert "DONE batch=" in log_text


def test_run_only_first_chunk_omits_append_flag(tmp_path):
    dest = tmp_path / "dest"
    names = [f"{i}.jpg" for i in range(4)]
    for n in names:
        _touch(dest / n)
    files_list = tmp_path / "files.txt"
    _write_files_list(files_list, names)

    seen_cmds = []

    def fake_runner(cmd, cwd, log_fh):
        seen_cmds.append(cmd)
        return 0

    photos_import.run(
        dest, files_list, "Album", dry_run=False, batch_dir=tmp_path / "batch",
        chunk_size=1, process_runner=fake_runner, **_MACOS_KWARGS,
    )

    assert "-O" not in seen_cmds[0]
    assert all("-O" in cmd for cmd in seen_cmds[1:])


def test_run_streams_process_output_into_the_log_file(tmp_path):
    dest = tmp_path / "dest"
    _touch(dest / "a.jpg")
    files_list = tmp_path / "files.txt"
    _write_files_list(files_list, ["a.jpg"])
    batch_dir = tmp_path / "batch"

    def fake_runner(cmd, cwd, log_fh):
        log_fh.write("osxphotos output line 1\n")
        log_fh.write("osxphotos output line 2\n")
        return 0

    photos_import.run(
        dest, files_list, "Album", dry_run=False, batch_dir=batch_dir,
        process_runner=fake_runner, **_MACOS_KWARGS,
    )

    log_text = (batch_dir / photos_import.DEFAULT_LOG_NAME).read_text()
    assert "osxphotos output line 1" in log_text
    assert "osxphotos output line 2" in log_text


# --- retry(): preflight ---

def test_retry_preflight_fails_on_invalid_mode(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    basenames = tmp_path / "basenames.txt"
    _write_files_list(basenames, ["a.jpg"])
    summary = photos_import.retry(
        dest, basenames, "Album", "bogus-mode", dry_run=True, batch_dir=tmp_path / "batch", **_MACOS_KWARGS
    )
    assert summary.counts["preflight_failed"] == 1


def test_retry_preflight_fails_on_non_macos(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    basenames = tmp_path / "basenames.txt"
    _write_files_list(basenames, ["a.jpg"])
    summary = photos_import.retry(
        dest, basenames, "Album", "skip-dups", dry_run=True, batch_dir=tmp_path / "batch",
        which_fn=lambda n: f"/usr/bin/{n}", platform_fn=lambda: "linux",
    )
    assert summary.counts["preflight_failed"] == 1


def test_retry_preflight_fails_when_a_listed_file_is_missing_from_dest(tmp_path):
    # basenames_file is typically a stale/hand-edited errors.txt or
    # missing.txt from photos-verify -- must fail clearly up front, same as
    # run()'s preflight(), rather than letting osxphotos hit it mid-retry.
    dest = tmp_path / "dest"
    _touch(dest / "a.jpg")
    basenames = tmp_path / "basenames.txt"
    _write_files_list(basenames, ["a.jpg", "gone.jpg"])
    summary = photos_import.retry(
        dest, basenames, "Album", "skip-dups", dry_run=True, batch_dir=tmp_path / "batch", **_MACOS_KWARGS
    )
    assert summary.counts["preflight_failed"] == 1


# --- retry(): one invocation per file ---

def test_retry_invokes_once_per_file(tmp_path):
    dest = tmp_path / "dest"
    names = ["a.jpg", "b.jpg", "c.jpg"]
    for n in names:
        _touch(dest / n)
    basenames = tmp_path / "basenames.txt"
    _write_files_list(basenames, names)
    batch_dir = tmp_path / "batch"

    calls = []

    def fake_runner(cmd, cwd, log_fh):
        calls.append(cmd)
        return 0

    summary = photos_import.retry(
        dest, basenames, "Album", "skip-dups", dry_run=False, batch_dir=batch_dir,
        process_runner=fake_runner, **_MACOS_KWARGS,
    )

    assert len(calls) == 3
    # Never batched -- each call imports exactly one file (see module
    # docstring lesson 5, the burst-group re-triggering risk).
    for cmd in calls:
        assert sum(1 for arg in cmd if arg.endswith(".jpg")) == 1
    assert summary.counts["files_retried"] == 3


def test_retry_dry_run_invokes_no_process(tmp_path):
    dest = tmp_path / "dest"
    _touch(dest / "a.jpg")
    basenames = tmp_path / "basenames.txt"
    _write_files_list(basenames, ["a.jpg"])

    calls = []
    photos_import.retry(
        dest, basenames, "Album", "skip-dups", dry_run=True, batch_dir=tmp_path / "batch",
        process_runner=lambda cmd, cwd, log_fh: calls.append(cmd) or 0,
        **_MACOS_KWARGS,
    )
    assert calls == []


def test_retry_shares_the_same_default_log_name_as_import(tmp_path):
    # retry_stragglers.sh was designed to append to the same log as
    # import_batch.sh for one continuous audit trail -- preserved here as
    # matching default filenames.
    assert photos_import.DEFAULT_LOG_NAME == "photos_import.log"
