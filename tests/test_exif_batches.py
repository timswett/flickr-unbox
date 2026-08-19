"""
Tests for flickr_unbox.exif_batches.

No existing fixture coverage for this stage (like flatten/merge_photoinfo
before it) -- built from scratch here, using small custom byte-size
thresholds rather than the real 40GB default so tests run fast and the
splitting logic can be exercised directly.
"""
from pathlib import Path

from flickr_unbox import exif_batches


def _make_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size_bytes)


def test_files_split_across_batches_by_size(tmp_path):
    dest = tmp_path
    _make_file(dest / "1.jpg", 40)
    _make_file(dest / "2.jpg", 40)
    _make_file(dest / "3.jpg", 40)  # 1+2 = 80 fits in 100; +3 = 120 would not

    batches = exif_batches.plan_batches(dest, batch_bytes=100)

    assert [name for name, _ in batches[0]] == ["1.jpg", "2.jpg"]
    assert [name for name, _ in batches[1]] == ["3.jpg"]


def test_single_oversized_file_gets_its_own_batch(tmp_path):
    dest = tmp_path
    _make_file(dest / "big.jpg", 500)

    batches = exif_batches.plan_batches(dest, batch_bytes=100)

    assert len(batches) == 1
    assert [name for name, _ in batches[0]] == ["big.jpg"]


def test_non_target_extensions_are_excluded(tmp_path):
    dest = tmp_path
    _make_file(dest / "1.jpg", 10)
    _make_file(dest / "1.json", 10)
    _make_file(dest / "readme.txt", 10)

    batches = exif_batches.plan_batches(dest, batch_bytes=1000)

    names = [name for batch in batches for name, _ in batch]
    assert names == ["1.jpg"]


def test_junk_files_are_excluded(tmp_path):
    dest = tmp_path
    _make_file(dest / "1.jpg", 10)
    _make_file(dest / "._1.jpg", 10)

    batches = exif_batches.plan_batches(dest, batch_bytes=1000)

    names = [name for batch in batches for name, _ in batch]
    assert names == ["1.jpg"]


def test_case_insensitive_extension_matching(tmp_path):
    dest = tmp_path
    _make_file(dest / "1.JPG", 10)
    _make_file(dest / "2.MOV", 10)

    batches = exif_batches.plan_batches(dest, batch_bytes=1000)

    names = {name for batch in batches for name, _ in batch}
    assert names == {"1.JPG", "2.MOV"}


def test_empty_dest_produces_zero_batches(tmp_path):
    dest = tmp_path
    batches = exif_batches.plan_batches(dest, batch_bytes=1000)
    assert batches == []


def test_dry_run_writes_no_batch_files(tmp_path):
    dest = tmp_path / "alldata"
    _make_file(dest / "1.jpg", 10)
    batch_dir = tmp_path / "batches"

    summary = exif_batches.run(dest, batch_dir, batch_bytes=1000, dry_run=True)

    assert summary.counts["target_files"] == 1
    assert not batch_dir.exists()


def test_no_dry_run_writes_batch_files_with_one_name_per_line(tmp_path):
    dest = tmp_path / "alldata"
    _make_file(dest / "1.jpg", 40)
    _make_file(dest / "2.jpg", 40)
    _make_file(dest / "3.jpg", 40)
    batch_dir = tmp_path / "batches"

    exif_batches.run(dest, batch_dir, batch_bytes=100, dry_run=False)

    assert (batch_dir / "batch_01.txt").read_text().splitlines() == ["1.jpg", "2.jpg"]
    assert (batch_dir / "batch_02.txt").read_text().splitlines() == ["3.jpg"]


def test_stale_batch_files_are_removed_on_replan(tmp_path):
    dest = tmp_path / "alldata"
    _make_file(dest / "1.jpg", 10)
    batch_dir = tmp_path / "batches"
    batch_dir.mkdir()
    (batch_dir / "batch_01.txt").write_text("old_stale_entry.jpg\n")
    (batch_dir / "batch_09.txt").write_text("leftover_from_bigger_run.jpg\n")

    summary = exif_batches.run(dest, batch_dir, batch_bytes=1000, dry_run=False)

    assert summary.counts["stale_batch_files_removed"] == 2
    assert not (batch_dir / "batch_09.txt").exists()
    assert (batch_dir / "batch_01.txt").read_text().splitlines() == ["1.jpg"]


def test_heic_and_heif_are_recognized_target_extensions(tmp_path):
    dest = tmp_path
    _make_file(dest / "1.heic", 10)
    _make_file(dest / "2.HEIF", 10)

    batches = exif_batches.plan_batches(dest, batch_bytes=1000)

    names = {name for batch in batches for name, _ in batch}
    assert names == {"1.heic", "2.HEIF"}


def test_run_reports_skipped_non_target_files(tmp_path):
    # A fixed extension allowlist can't cover every format a real export
    # might contain -- previously these were silently dropped from every
    # batch forever with no signal to the operator. Now surfaced explicitly.
    dest = tmp_path / "alldata"
    _make_file(dest / "1.jpg", 10)
    _make_file(dest / "1.json", 10)  # sidecar, correctly not "skipped"
    _make_file(dest / "clip.avi", 10)
    _make_file(dest / "scan.tiff", 10)
    batch_dir = tmp_path / "batches"

    summary = exif_batches.run(dest, batch_dir, batch_bytes=1000, dry_run=True)

    assert summary.counts["target_files"] == 1
    assert summary.counts["skipped_non_target_files"] == 2
    assert any(".avi" in n and ".tiff" in n for n in summary.notes)


def test_run_reports_zero_skipped_when_nothing_is_excluded(tmp_path):
    dest = tmp_path / "alldata"
    _make_file(dest / "1.jpg", 10)
    batch_dir = tmp_path / "batches"

    summary = exif_batches.run(dest, batch_dir, batch_bytes=1000, dry_run=True)

    assert summary.counts["skipped_non_target_files"] == 0


def test_preflight_fails_on_missing_dest(tmp_path):
    summary = exif_batches.run(
        tmp_path / "does_not_exist", tmp_path / "batches", batch_bytes=1000, dry_run=True
    )
    assert summary.counts["preflight_failed"] == 1
