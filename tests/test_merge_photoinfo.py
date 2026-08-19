"""
Tests for flickr_unbox.merge_photoinfo.

merge_photoinfo shares its collision/move/cleanup logic with flatten via
_collision_merge (see test_flatten.py for coverage of that shared
machinery: collision-prefix renaming, conflict-on-conflict skip,
empty-dir cleanup, dry-run no-op, preflight failure). These tests instead
focus on what's actually distinct about this stage: only *.json files are
merged (non-JSON files in a part folder are left alone), and the extra
belt-and-suspenders junk sweep on dest itself after the merge.
"""
from pathlib import Path

from flickr_unbox import merge_photoinfo


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_only_json_files_are_merged(tmp_path):
    source_base = tmp_path / "PhotoInformation"
    dest = tmp_path / "alldata"
    _touch(source_base / "acct_part2" / "photo_111.json", "{}")
    _touch(source_base / "acct_part2" / "readme.txt", "not json")

    summary = merge_photoinfo.run(source_base, dest, "*part*", dry_run=False)

    assert summary.counts["moved"] == 1
    assert (dest / "photo_111.json").exists()
    assert not (dest / "readme.txt").exists()
    assert (source_base / "acct_part2" / "readme.txt").exists()  # left in place


def test_collision_gets_part_folder_prefix(tmp_path):
    source_base = tmp_path / "PhotoInformation"
    dest = tmp_path / "alldata"
    _touch(source_base / "acct_part2" / "photo_111.json", "first")
    _touch(source_base / "acct_part3" / "photo_111.json", "second")

    summary = merge_photoinfo.run(source_base, dest, "*part*", dry_run=False)

    assert summary.counts["collision_renamed"] == 1
    assert (dest / "photo_111.json").read_text() == "first"
    assert (dest / "acct_part3_photo_111.json").read_text() == "second"


def test_dest_junk_swept_after_merge(tmp_path):
    source_base = tmp_path / "PhotoInformation"
    dest = tmp_path / "alldata"
    dest.mkdir(parents=True)
    _touch(dest / ".DS_Store", "junk")
    _touch(source_base / "acct_part2" / "photo_111.json", "{}")

    summary = merge_photoinfo.run(source_base, dest, "*part*", dry_run=False)

    assert summary.counts["dest_junk_swept"] == 1
    assert not (dest / ".DS_Store").exists()


def test_dry_run_leaves_dest_junk_in_place(tmp_path):
    source_base = tmp_path / "PhotoInformation"
    dest = tmp_path / "alldata"
    dest.mkdir(parents=True)
    _touch(dest / ".DS_Store", "junk")
    _touch(source_base / "acct_part2" / "photo_111.json", "{}")

    summary = merge_photoinfo.run(source_base, dest, "*part*", dry_run=True)

    assert "dest_junk_swept" not in summary.counts
    assert (dest / ".DS_Store").exists()
