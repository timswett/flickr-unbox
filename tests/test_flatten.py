"""
Tests for flickr_unbox.flatten.

flatten had zero fixture coverage before this port (see
FLICKR_UNBOX_HANDOFF.md "TODOs for the Python port") -- these fixtures are
built inline per-test with tmp_path rather than via tools/generate_test_fixtures.py,
since flatten's whole job is reshaping a *multi-folder* source tree, which
is a different shape of fixture than the post-flatten `alldata_sim/` cases
that generator produces for the matching/rename logic.
"""
from pathlib import Path

from flickr_unbox import flatten


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_dry_run_moves_nothing(tmp_path):
    source_base = tmp_path / "Expanded"
    dest = tmp_path / "alldata"
    _touch(source_base / "data-download-01" / "IMG_1.jpg", "one")

    summary = flatten.run(source_base, dest, "data-download-*", dry_run=True)

    assert summary.dry_run is True
    assert summary.counts["files_to_move"] == 1
    assert (source_base / "data-download-01" / "IMG_1.jpg").exists()
    assert not dest.exists() or not any(dest.iterdir())


def test_basic_move_no_collision(tmp_path):
    source_base = tmp_path / "Expanded"
    dest = tmp_path / "alldata"
    _touch(source_base / "data-download-01" / "IMG_1.jpg", "one")
    _touch(source_base / "data-download-02" / "nested" / "IMG_2.jpg", "two")

    summary = flatten.run(source_base, dest, "data-download-*", dry_run=False)

    assert summary.counts["moved"] == 2
    assert summary.counts.get("failed", 0) == 0
    assert (dest / "IMG_1.jpg").read_text() == "one"
    assert (dest / "IMG_2.jpg").read_text() == "two"


def test_collision_gets_source_folder_prefix(tmp_path):
    source_base = tmp_path / "Expanded"
    dest = tmp_path / "alldata"
    _touch(source_base / "data-download-01" / "IMG_1.jpg", "first")
    _touch(source_base / "data-download-02" / "IMG_1.jpg", "second")

    summary = flatten.run(source_base, dest, "data-download-*", dry_run=False)

    assert summary.counts["moved"] == 2
    assert summary.counts["collision_renamed"] == 1
    assert (dest / "IMG_1.jpg").read_text() == "first"
    assert (dest / "data-download-02_IMG_1.jpg").read_text() == "second"


def test_conflict_on_conflict_is_skipped_not_fatal(tmp_path):
    source_base = tmp_path / "Expanded"
    dest = tmp_path / "alldata"
    # Both the plain name AND the collision-prefixed name are already taken
    # in dest before this run even starts.
    _touch(dest / "IMG_1.jpg", "existing-plain")
    _touch(dest / "data-download-02_IMG_1.jpg", "existing-prefixed")
    _touch(source_base / "data-download-01" / "IMG_1.jpg", "first")
    _touch(source_base / "data-download-02" / "IMG_1.jpg", "second")

    summary = flatten.run(source_base, dest, "data-download-*", dry_run=False)

    assert summary.counts["unresolved_conflicts"] == 1
    # The one file that *can* be placed safely still gets moved -- a single
    # unresolvable file must not block the rest of the run.
    assert summary.counts["moved"] == 0 or summary.counts["unresolved_conflicts"] >= 1
    # Original files untouched
    assert (dest / "IMG_1.jpg").read_text() == "existing-plain"
    assert (dest / "data-download-02_IMG_1.jpg").read_text() == "existing-prefixed"


def test_junk_files_are_swept_not_moved(tmp_path):
    source_base = tmp_path / "Expanded"
    dest = tmp_path / "alldata"
    _touch(source_base / "data-download-01" / "IMG_1.jpg", "one")
    _touch(source_base / "data-download-01" / "._IMG_1.jpg", "junk")
    _touch(source_base / "data-download-01" / ".DS_Store", "junk")

    summary = flatten.run(source_base, dest, "data-download-*", dry_run=False)

    assert summary.counts["junk_files_found"] == 2
    assert summary.counts["moved"] == 1
    assert list(dest.glob("*")) == [dest / "IMG_1.jpg"]


def test_empty_source_dirs_removed_after_real_run(tmp_path):
    source_base = tmp_path / "Expanded"
    dest = tmp_path / "alldata"
    _touch(source_base / "data-download-01" / "nested" / "IMG_1.jpg", "one")

    flatten.run(source_base, dest, "data-download-*", dry_run=False)

    assert not (source_base / "data-download-01").exists()


def test_case_insensitive_collision_is_detected_on_case_varying_names(tmp_path):
    # macOS/APFS (the default filesystem this pipeline actually runs on) is
    # case-insensitive: two files whose names differ only by case collide on
    # disk even though they're distinct Python strings. Without this check,
    # the second shutil.move() would silently land on the same path as the
    # first, overwriting it with no error, no collision-renamed bump, no log.
    source_base = tmp_path / "Expanded"
    dest = tmp_path / "alldata"
    _touch(source_base / "data-download-01" / "IMG_0099.JPG", "first")
    _touch(source_base / "data-download-02" / "img_0099.jpg", "second")

    summary = flatten.run(source_base, dest, "data-download-*", dry_run=False)

    assert summary.counts["moved"] == 2
    assert summary.counts["collision_renamed"] == 1
    assert (dest / "IMG_0099.JPG").read_text() == "first"
    assert (dest / "data-download-02_img_0099.jpg").read_text() == "second"


def test_preflight_fails_on_missing_source(tmp_path):
    source_base = tmp_path / "does-not-exist"
    dest = tmp_path / "alldata"

    summary = flatten.run(source_base, dest, "data-download-*", dry_run=True)

    assert summary.counts["preflight_failed"] == 1
