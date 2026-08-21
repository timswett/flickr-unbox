"""
Tests for flickr_unbox.fix_media_dates.

No real osxphotos/exiftool/photoscript needed: query_fn, tag_reader_fn,
and date_setter_fn are all injectable, so the whole DATE_TAGS fallback
chain and the dry-run/real-write distinction can be exercised without any
real dependency installed.
"""
from datetime import datetime
from pathlib import Path

from flickr_unbox import fix_media_dates

_MACOS_KWARGS = dict(which_fn=lambda name: f"/usr/bin/{name}", platform_fn=lambda: "darwin")


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


# --- read_exif_date(): the DATE_TAGS fallback chain ---

def test_read_exif_date_uses_datetimeoriginal_when_not_suspicious():
    tags = {"DateTimeOriginal": "2018:01:01 10:00:00"}
    reading = fix_media_dates.read_exif_date(
        Path("a.jpg"), datetime(2026, 8, 1), lambda path, tag: tags.get(tag)
    )
    assert reading == (datetime(2018, 1, 1, 10, 0, 0), "DateTimeOriginal")


def test_read_exif_date_falls_back_to_createdate_when_datetimeoriginal_missing():
    tags = {"CreateDate": "2019:03:03 09:00:00"}
    reading = fix_media_dates.read_exif_date(
        Path("a.gif"), datetime(2026, 8, 1), lambda path, tag: tags.get(tag)
    )
    assert reading == (datetime(2019, 3, 3, 9, 0, 0), "CreateDate")


def test_read_exif_date_falls_back_to_mediacreatedate_when_others_corrupted():
    # The real case this fallback exists for: DateTimeOriginal and
    # CreateDate both hold a bogus placeholder still inside the suspect
    # window, MediaCreateDate (QuickTime-container-specific) holds the
    # real date.
    tags = {
        "DateTimeOriginal": "2036:01:01 23:59:59",
        "CreateDate": "2036:01:01 23:59:59",
        "MediaCreateDate": "2019:01:16 20:51:54",
    }
    reading = fix_media_dates.read_exif_date(
        Path("a.mp4"), datetime(2026, 8, 1), lambda path, tag: tags.get(tag)
    )
    assert reading == (datetime(2019, 1, 16, 20, 51, 54), "MediaCreateDate")


def test_read_exif_date_returns_last_reading_when_all_tags_still_suspicious():
    tags = {
        "DateTimeOriginal": "2026:08:15 00:00:00",
        "CreateDate": "2026:08:15 00:00:00",
        "MediaCreateDate": "2026:08:15 00:00:00",
    }
    reading = fix_media_dates.read_exif_date(
        Path("a.mp4"), datetime(2026, 8, 1), lambda path, tag: tags.get(tag)
    )
    # Still returns a value (the last one tried) rather than None -- caller
    # is responsible for the "** WARNING **" note when it's still suspicious.
    assert reading == (datetime(2026, 8, 15, 0, 0, 0), "MediaCreateDate")


def test_read_exif_date_returns_none_when_no_tag_is_readable():
    reading = fix_media_dates.read_exif_date(Path("a.jpg"), datetime(2026, 8, 1), lambda path, tag: None)
    assert reading is None


def test_read_exif_date_skips_unparseable_values():
    tags = {"DateTimeOriginal": "not-a-date", "CreateDate": "2019:03:03 09:00:00"}
    reading = fix_media_dates.read_exif_date(
        Path("a.jpg"), datetime(2026, 8, 1), lambda path, tag: tags.get(tag)
    )
    assert reading == (datetime(2019, 3, 3, 9, 0, 0), "CreateDate")


# --- run(): preflight ---

def test_run_preflight_fails_on_non_macos(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    summary = fix_media_dates.run(
        "Album", source_dir, "2026-08-01", dry_run=True,
        which_fn=lambda n: f"/usr/bin/{n}", platform_fn=lambda: "linux",
    )
    assert summary.counts["preflight_failed"] == 1


def test_run_preflight_fails_when_source_dir_missing(tmp_path):
    summary = fix_media_dates.run(
        "Album", tmp_path / "nope", "2026-08-01", dry_run=True, **_MACOS_KWARGS
    )
    assert summary.counts["preflight_failed"] == 1


# --- run(): dry run never calls date_setter_fn ---

def test_run_dry_run_never_calls_date_setter(tmp_path):
    source_dir = tmp_path / "src"
    _touch(source_dir / "a.gif")

    calls = []
    summary = fix_media_dates.run(
        "Album", source_dir, "2026-08-01", dry_run=True,
        query_fn=lambda bin_, album, after: [
            {"original_filename": "a.gif", "uuid": "U1", "date": "2026-08-15T00:00:00"}
        ],
        tag_reader_fn=lambda path, tag: {"DateTimeOriginal": "2018:06:15 10:00:00"}.get(tag),
        date_setter_fn=lambda uuid, new_date: calls.append((uuid, new_date)),
        **_MACOS_KWARGS,
    )

    assert calls == []
    assert summary.counts["would_fix"] == 1


def test_run_real_run_calls_date_setter_with_the_corrected_date(tmp_path):
    source_dir = tmp_path / "src"
    _touch(source_dir / "a.gif")

    calls = []
    summary = fix_media_dates.run(
        "Album", source_dir, "2026-08-01", dry_run=False,
        query_fn=lambda bin_, album, after: [
            {"original_filename": "a.gif", "uuid": "U1", "date": "2026-08-15T00:00:00"}
        ],
        tag_reader_fn=lambda path, tag: {"DateTimeOriginal": "2018:06:15 10:00:00"}.get(tag),
        date_setter_fn=lambda uuid, new_date: calls.append((uuid, new_date)),
        **_MACOS_KWARGS,
    )

    assert calls == [("U1", datetime(2018, 6, 15, 10, 0, 0))]
    assert summary.counts["fixed"] == 1


def test_run_skips_item_with_missing_filename_or_uuid(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()

    summary = fix_media_dates.run(
        "Album", source_dir, "2026-08-01", dry_run=False,
        query_fn=lambda bin_, album, after: [{"date": "2026-08-15T00:00:00"}],  # no filename/uuid
        tag_reader_fn=lambda path, tag: None,
        date_setter_fn=lambda uuid, new_date: None,
        **_MACOS_KWARGS,
    )
    assert summary.counts["skipped"] == 1


def test_run_skips_item_whose_source_file_is_missing(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()  # a.gif deliberately not created

    summary = fix_media_dates.run(
        "Album", source_dir, "2026-08-01", dry_run=False,
        query_fn=lambda bin_, album, after: [
            {"original_filename": "a.gif", "uuid": "U1", "date": "2026-08-15T00:00:00"}
        ],
        tag_reader_fn=lambda path, tag: None,
        date_setter_fn=lambda uuid, new_date: None,
        **_MACOS_KWARGS,
    )
    assert summary.counts["skipped"] == 1
    assert any("source file not found" in n for n in summary.notes)


def test_run_warns_when_even_the_fallback_date_is_still_suspicious(tmp_path):
    source_dir = tmp_path / "src"
    _touch(source_dir / "a.mp4")

    summary = fix_media_dates.run(
        "Album", source_dir, "2026-08-01", dry_run=True,
        query_fn=lambda bin_, album, after: [
            {"original_filename": "a.mp4", "uuid": "U1", "date": "2026-08-15T00:00:00"}
        ],
        tag_reader_fn=lambda path, tag: "2026:08:20 00:00:00",  # every tag still in the suspect window
        date_setter_fn=lambda uuid, new_date: None,
        **_MACOS_KWARGS,
    )
    assert any("WARNING" in n for n in summary.notes)
