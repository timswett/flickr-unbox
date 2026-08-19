"""
Tests for flickr_unbox.exif_write.

No real exiftool binary or real disk-space state is needed here: the
free-space check, the exiftool-on-PATH check, and the exiftool invocation
itself are all injectable (disk_usage_fn, which_fn, process_runner). The
parse_output() tests use text excerpted directly from the real
exif_batch_01.log/exif_batch_03.log produced by the actual bash migration,
not guessed formats, so a real-format regression would be caught here.
Validating against a real exiftool binary and real files is a separate ad
hoc pass (see FLICKR_UNBOX_HANDOFF.md), not part of this suite.
"""
import json
from collections import namedtuple
from pathlib import Path

from flickr_unbox import exif_write

# Verbatim excerpt from exif_batch_01.log (0 errors, 1 benign warning)
REAL_SUCCESS_OUTPUT = """\
======== 47004786621.jpg [12237/12237]
Warning: [Minor] Duplicate XMP property: mwg-rs:Regions/mwg-rs:AppliedToDimensions/stDim:unit - 47004786621.jpg
12237 image files updated
"""

# Verbatim excerpt from exif_batch_03.log (2 real errors + benign warnings)
REAL_ERROR_OUTPUT = """\
======== 51394763994.jpg [2814/2824]
Warning: [Minor] Duplicate XMP property: mwg-rs:Regions/mwg-rs:AppliedToDimensions/stDim:unit - 51394763994.jpg
======== 50531875228.png [1121/2824]
Error: [minor] IFD0 pointer references previous IFD0 directory - 50531875228.png
======== 50531875233.png [1122/2824]
Error: [minor] IFD0 pointer references previous IFD0 directory - 50531875233.png
 2822 image files updated
    2 files weren't updated due to errors
"""

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def _touch(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_batch_file(path: Path, names) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(names) + "\n")


# --- parse_output(): grounded in real captured log excerpts ---

def test_parses_real_success_output():
    result = exif_write.parse_output(REAL_SUCCESS_OUTPUT)
    assert result.files_updated == 12237
    assert result.files_errored == 0
    assert result.warnings == 1
    assert result.error_lines == []


def test_parses_real_error_output():
    result = exif_write.parse_output(REAL_ERROR_OUTPUT)
    assert result.files_updated == 2822
    assert result.files_errored == 2
    assert result.warnings == 1
    assert result.error_lines == [
        ("[minor] IFD0 pointer references previous IFD0 directory", "50531875228.png"),
        ("[minor] IFD0 pointer references previous IFD0 directory", "50531875233.png"),
    ]


def test_parse_output_defaults_to_zero_on_empty_text():
    result = exif_write.parse_output("")
    assert result.files_updated == 0
    assert result.files_errored == 0
    assert result.warnings == 0


def test_parse_output_recognizes_unchanged_files_line():
    # A file with nothing exiftool could write (e.g. a sidecar with no
    # writable fields) shows up in exiftool's own summary as "unchanged",
    # a third outcome distinct from "updated"/"weren't updated due to
    # errors" that the original exact-match regex never accounted for.
    text = "  1 image files updated\n  1 image files unchanged\n"
    result = exif_write.parse_output(text)
    assert result.files_updated == 1
    assert result.files_unchanged == 1
    assert result.files_errored == 0


def test_parse_output_sums_multiple_updated_lines():
    # Broadened from an exact "image files updated" match to sum every
    # "N <word> files updated" line, in case a batch ever produces more
    # than one category line (e.g. a hypothetical separate video count).
    text = "  3 image files updated\n  2 video files updated\n"
    result = exif_write.parse_output(text)
    assert result.files_updated == 5


# --- check_free_space(): injectable disk usage, no real disk touched ---

def test_free_space_check_passes_when_plenty_available(tmp_path):
    result = exif_write.check_free_space(
        tmp_path, batch_bytes=100, disk_usage_fn=lambda p: _DiskUsage(1000, 0, 1000)
    )
    assert result.ok


def test_free_space_check_fails_when_insufficient(tmp_path):
    # batch is 100 bytes, needs 1.2x = 120, only 100 available
    result = exif_write.check_free_space(
        tmp_path, batch_bytes=100, disk_usage_fn=lambda p: _DiskUsage(1000, 900, 100)
    )
    assert not result.ok
    assert "not enough free space" in result.reasons[0]


# --- preflight(): all checks combined ---

def test_preflight_fails_on_missing_batch_file(tmp_path):
    result = exif_write.preflight(tmp_path, tmp_path / "batch_01.txt", "exiftool")
    assert not result.ok
    assert "batch file not found" in result.reasons[0]


def test_preflight_fails_when_exiftool_not_on_path(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg")
    batch_path = tmp_path / "batch_01.txt"
    _write_batch_file(batch_path, ["111.jpg"])

    result = exif_write.preflight(
        dest, batch_path, "exiftool",
        disk_usage_fn=lambda p: _DiskUsage(0, 0, 10**12),
        which_fn=lambda name: None,
    )
    assert not result.ok
    assert "not found on PATH" in result.reasons[0]


def test_preflight_fails_when_batch_lists_a_missing_file(tmp_path):
    dest = tmp_path / "alldata"
    dest.mkdir()
    batch_path = tmp_path / "batch_01.txt"
    _write_batch_file(batch_path, ["gone.jpg"])

    result = exif_write.preflight(
        dest, batch_path, "exiftool",
        disk_usage_fn=lambda p: _DiskUsage(0, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
    )
    assert not result.ok
    assert "missing from dest" in result.reasons[0]


def test_preflight_fails_on_insufficient_space_via_injected_disk_usage(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg", b"x" * 100)
    batch_path = tmp_path / "batch_01.txt"
    _write_batch_file(batch_path, ["111.jpg"])

    result = exif_write.preflight(
        dest, batch_path, "exiftool",
        disk_usage_fn=lambda p: _DiskUsage(1000, 990, 10),
        which_fn=lambda name: "/usr/bin/exiftool",
    )
    assert not result.ok
    assert "not enough free space" in result.reasons[0]


def test_preflight_passes_with_all_injected_checks_green(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg", b"x" * 100)
    batch_path = tmp_path / "batch_01.txt"
    _write_batch_file(batch_path, ["111.jpg"])

    result = exif_write.preflight(
        dest, batch_path, "exiftool",
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
    )
    assert result.ok


# --- run(): dry-run never invokes exiftool; real run does, and writes a receipt ---

def test_dry_run_never_invokes_exiftool(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg", b"x" * 100)
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", ["111.jpg"])

    calls = []
    summary = exif_write.run(
        dest, batch_dir, "01", dry_run=True,
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
        process_runner=lambda cmd, cwd: (calls.append((cmd, cwd)) or "", 0),
    )

    assert calls == []
    assert summary.counts["batch_files"] == 1
    assert not (batch_dir / "batch_01.result.json").exists()


def test_real_run_invokes_exiftool_with_dest_as_cwd_and_writes_receipt(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg", b"x" * 100)
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", ["111.jpg"])

    calls = []

    def fake_runner(cmd, cwd):
        calls.append((cmd, cwd))
        return REAL_SUCCESS_OUTPUT.replace("12237", "1"), 0

    summary = exif_write.run(
        dest, batch_dir, "01", dry_run=False,
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
        process_runner=fake_runner,
    )

    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cwd == dest
    assert cmd[0] == "exiftool"
    assert "-@" in cmd

    assert summary.counts["files_updated"] == 1
    assert summary.counts["files_errored"] == 0

    receipt_path = batch_dir / "batch_01.result.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["files_updated"] == 1
    assert receipt["files_errored"] == 0
    assert receipt["batch_num"] == "01"


def test_real_run_with_errors_logs_each_error_line(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "1.png", b"x")
    _touch(dest / "2.png", b"x")
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_03.txt", ["1.png", "2.png"])

    summary = exif_write.run(
        dest, batch_dir, "03", dry_run=False,
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
        process_runner=lambda cmd, cwd: (REAL_ERROR_OUTPUT, 0),
    )

    assert summary.counts["files_errored"] == 2
    error_notes = [n for n in summary.notes if n.startswith("ERROR:")]
    assert len(error_notes) == 2
    assert "50531875228.png" in error_notes[0]

    receipt = json.loads((batch_dir / "batch_03.result.json").read_text())
    assert receipt["files_errored"] == 2


def test_junk_files_swept_before_real_run_not_before_dry_run(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg", b"x")
    _touch(dest / ".DS_Store", b"junk")
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", ["111.jpg"])
    common = dict(
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
    )

    exif_write.run(dest, batch_dir, "01", dry_run=True, process_runner=lambda c, w: ("", 0), **common)
    assert (dest / ".DS_Store").exists()

    exif_write.run(
        dest, batch_dir, "01", dry_run=False,
        process_runner=lambda c, w: (REAL_SUCCESS_OUTPUT.replace("12237", "1"), 0), **common,
    )
    assert not (dest / ".DS_Store").exists()


def test_nonzero_returncode_is_a_hard_failure_no_receipt_written(tmp_path):
    # A crashed/killed exiftool process (OOM, disk full, SIGTERM) may print
    # no "files updated"/"files weren't updated" line at all. Without a
    # returncode check, parse_output() would report 0 errors -- a 0-error
    # receipt that could satisfy cleanup's safety gate despite the batch
    # never actually being written.
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg", b"x" * 100)
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", ["111.jpg"])

    summary = exif_write.run(
        dest, batch_dir, "01", dry_run=False,
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
        process_runner=lambda cmd, cwd: ("partial output, no summary line\n", -9),
    )

    assert summary.counts["process_failed"] == 1
    assert "files_updated" not in summary.counts
    assert not (batch_dir / "batch_01.result.json").exists()


def test_accounted_for_includes_unchanged_files_no_false_stale_note(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "1.jpg", b"x")
    _touch(dest / "2.jpg", b"x")
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", ["1.jpg", "2.jpg"])

    output = "  1 image files updated\n  1 image files unchanged\n"
    summary = exif_write.run(
        dest, batch_dir, "01", dry_run=False,
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
        process_runner=lambda cmd, cwd: (output, 0),
    )

    assert summary.counts["files_updated"] == 1
    assert summary.counts["files_unchanged"] == 1
    assert not any(n.startswith("NOTE: batch had") for n in summary.notes)

    receipt = json.loads((batch_dir / "batch_01.result.json").read_text())
    assert receipt["files_unchanged"] == 1


# --- check_sidecar_schema(): catches a schema mismatch before invoking exiftool ---

def test_schema_check_passes_when_sidecars_have_the_expected_key(tmp_path):
    dest = tmp_path
    _touch(dest / "111.json", json.dumps({"Name": "a title", "Description": ""}).encode())

    result = exif_write.check_sidecar_schema(dest, ["111.jpg"])
    assert result.ok


def test_schema_check_matches_key_case_insensitively(tmp_path):
    # Real Flickr sidecars use lowercase "name", not "Name" -- exiftool's
    # own -tagsfromfile matching is case-insensitive, so this check must be
    # too, or it flags real, already-working data as a schema mismatch
    # (caught by an ad hoc real-data smoke test, not by the synthetic suite
    # alone -- the synthetic fixtures below all happened to use "Name").
    dest = tmp_path
    _touch(dest / "111.json", json.dumps({"name": "a title", "description": ""}).encode())

    result = exif_write.check_sidecar_schema(dest, ["111.jpg"])
    assert result.ok


def test_schema_check_fails_when_no_sampled_sidecar_has_the_expected_key(tmp_path):
    dest = tmp_path
    # A structurally different schema -- e.g. lowercase "name" instead of
    # "Name" -- would make -tagsfromfile silently write nothing at all.
    _touch(dest / "111.json", json.dumps({"title": "a title", "desc": ""}).encode())

    result = exif_write.check_sidecar_schema(dest, ["111.jpg"])
    assert not result.ok
    assert "Name" in result.reasons[0]


def test_schema_check_passes_when_no_sidecars_are_readable():
    # Nothing to sample -- don't block on this check alone; the missing-file
    # preflight check already covers a genuinely stale batch plan.
    result = exif_write.check_sidecar_schema(Path("/nonexistent"), ["111.jpg"])
    assert result.ok


def test_run_refuses_on_schema_mismatch_before_invoking_exiftool(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "111.jpg", b"x" * 10)
    _touch(dest / "111.json", json.dumps({"title": "wrong schema"}).encode())
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", ["111.jpg"])

    calls = []
    summary = exif_write.run(
        dest, batch_dir, "01", dry_run=True,
        disk_usage_fn=lambda p: _DiskUsage(10**12, 0, 10**12),
        which_fn=lambda name: "/usr/bin/exiftool",
        process_runner=lambda cmd, cwd: calls.append(1) or ("", 0),
    )

    assert summary.counts["preflight_failed"] == 1
    assert calls == []
    assert any("different Flickr export schema" in n for n in summary.notes)
