"""
Tests for flickr_unbox.photos_diff.

No real osxphotos/exiftool binary or real Photos library needed: the
subprocess calls are injectable (process_runner), so both the Apple
Photos library query and the exiftool date read are fed canned text.
There's no real captured `osxphotos query --json` sample in this repo
(see photos_diff.py's module docstring) -- fixtures here are hand-built
from the documented field names (uuid, date, original_filename) rather
than a real excerpt.
"""
import json
from datetime import datetime
from pathlib import Path

from flickr_unbox import photos_diff


def _touch(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# --- parse_library_records() ---

def test_parse_library_records_sorts_by_date():
    items = [
        {"uuid": "B", "date": "2020-06-15T12:00:00-04:00", "original_filename": "b.jpg"},
        {"uuid": "A", "date": "2018-01-01T00:00:00-04:00", "original_filename": "a.jpg"},
    ]
    records, skipped = photos_diff.parse_library_records(items)
    assert skipped == 0
    assert [r.uuid for r in records] == ["A", "B"]


def test_parse_library_records_skips_missing_uuid_or_date():
    items = [
        {"uuid": "A", "date": "2018-01-01T00:00:00-04:00"},
        {"date": "2018-01-01T00:00:00-04:00", "original_filename": "no_uuid.jpg"},
        {"uuid": "B", "original_filename": "no_date.jpg"},
    ]
    records, skipped = photos_diff.parse_library_records(items)
    assert len(records) == 1
    assert skipped == 2


def test_parse_library_records_skips_unparseable_date():
    items = [{"uuid": "A", "date": "not-a-date"}]
    records, skipped = photos_diff.parse_library_records(items)
    assert records == []
    assert skipped == 1


def test_parse_library_records_strips_timezone_for_naive_comparison():
    items = [{"uuid": "A", "date": "2018-01-01T10:00:00+05:00"}]
    records, _ = photos_diff.parse_library_records(items)
    assert records[0].date.tzinfo is None
    assert records[0].date == datetime(2018, 1, 1, 10, 0, 0)


def test_load_photos_library_raises_on_nonzero_returncode():
    import pytest

    with pytest.raises(RuntimeError, match="osxphotos query failed"):
        photos_diff.load_photos_library("osxphotos", process_runner=lambda cmd, cwd: ("boom", 1))


def test_load_photos_library_parses_injected_json():
    payload = json.dumps([{"uuid": "A", "date": "2018-01-01T00:00:00-04:00", "original_filename": "a.jpg"}])
    records, skipped = photos_diff.load_photos_library(
        "osxphotos", process_runner=lambda cmd, cwd: (payload, 0)
    )
    assert len(records) == 1
    assert skipped == 0


# --- parse_source_dates_json() / read_source_dates() ---

def test_parse_source_dates_json_prefers_datetimeoriginal():
    text = json.dumps([
        {"SourceFile": "a.jpg", "DateTimeOriginal": "2018:01:01 10:00:00", "CreateDate": "2019:01:01 10:00:00"},
    ])
    dates = photos_diff.parse_source_dates_json(text)
    assert dates["a.jpg"] == datetime(2018, 1, 1, 10, 0, 0)


def test_parse_source_dates_json_falls_back_to_createdate():
    text = json.dumps([{"SourceFile": "a.jpg", "CreateDate": "2019:01:01 10:00:00"}])
    dates = photos_diff.parse_source_dates_json(text)
    assert dates["a.jpg"] == datetime(2019, 1, 1, 10, 0, 0)


def test_parse_source_dates_json_none_when_neither_tag_present():
    text = json.dumps([{"SourceFile": "a.jpg"}])
    dates = photos_diff.parse_source_dates_json(text)
    assert dates["a.jpg"] is None


def test_read_source_dates_fills_missing_entries_with_none(tmp_path):
    dest = tmp_path / "dest"
    _touch(dest / "a.jpg")
    _touch(dest / "b.jpg")

    seen_argfiles = []

    def fake_runner(cmd, cwd):
        argfile_path = Path(cmd[-1])
        seen_argfiles.append(argfile_path)
        assert argfile_path.exists()  # readable at call time
        assert argfile_path.read_text() == "a.jpg\nb.jpg\n"
        # exiftool's own output only mentions a.jpg -- b.jpg must still end
        # up bucketed as "no timestamp", not silently dropped.
        return json.dumps([{"SourceFile": "a.jpg", "DateTimeOriginal": "2018:01:01 10:00:00"}]), 0

    dates = photos_diff.read_source_dates(dest, ["a.jpg", "b.jpg"], "exiftool", fake_runner)

    assert dates["a.jpg"] == datetime(2018, 1, 1, 10, 0, 0)
    assert dates["b.jpg"] is None
    assert not seen_argfiles[0].exists()  # cleaned up after the call, and not under any user-visible dir


def test_read_source_dates_raises_on_nonzero_returncode(tmp_path):
    import pytest

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(RuntimeError, match="exiftool batch date read failed"):
        photos_diff.read_source_dates(dest, ["a.jpg"], "exiftool", lambda cmd, cwd: ("boom", 1))


# --- match(): the two-tier window logic ---

def _library(*uuids_and_dates):
    records = [
        photos_diff.PhotoRecord(uuid=u, date=d, original_filename=None) for u, d in uuids_and_dates
    ]
    records.sort(key=lambda r: r.date)
    return records, [r.date for r in records]


def test_match_exact_timestamp_is_confirmed():
    library, dates = _library(("A", datetime(2020, 1, 1, 12, 0, 0)))
    result = photos_diff.match(datetime(2020, 1, 1, 12, 0, 0), library, dates)
    assert result.status == "confirmed"
    assert result.uuid == "A"
    assert result.delta_seconds == 0


def test_match_just_inside_tight_window_is_confirmed():
    library, dates = _library(("A", datetime(2020, 1, 1, 12, 0, 5)))
    result = photos_diff.match(datetime(2020, 1, 1, 12, 0, 0), library, dates, tight_s=5)
    assert result.status == "confirmed"


def test_match_just_outside_tight_window_is_needs_review():
    library, dates = _library(("A", datetime(2020, 1, 1, 12, 0, 6)))
    result = photos_diff.match(datetime(2020, 1, 1, 12, 0, 0), library, dates, tight_s=5, loose_s=120)
    assert result.status == "needs_review"
    assert result.uuid == "A"


def test_match_just_outside_loose_window_is_missing():
    library, dates = _library(("A", datetime(2020, 1, 1, 12, 2, 1)))  # 121s away
    result = photos_diff.match(datetime(2020, 1, 1, 12, 0, 0), library, dates, tight_s=5, loose_s=120)
    assert result.status == "missing"
    assert result.uuid is None


def test_match_picks_nearest_of_multiple_candidates():
    library, dates = _library(
        ("far_before", datetime(2020, 1, 1, 11, 0, 0)),
        ("near", datetime(2020, 1, 1, 12, 0, 3)),
        ("far_after", datetime(2020, 1, 1, 13, 0, 0)),
    )
    result = photos_diff.match(datetime(2020, 1, 1, 12, 0, 0), library, dates)
    assert result.uuid == "near"
    assert result.status == "confirmed"


def test_match_empty_library_is_missing():
    result = photos_diff.match(datetime(2020, 1, 1, 12, 0, 0), [], [])
    assert result.status == "missing"


# --- run(): end to end with injected process_runner ---

def test_run_buckets_files_correctly(tmp_path):
    dest = tmp_path / "dest"
    _touch(dest / "confirmed.jpg")
    _touch(dest / "review.jpg")
    _touch(dest / "missing.jpg")
    _touch(dest / "no_ts.jpg")
    out_dir = tmp_path / "out"

    library_json = json.dumps([
        {"uuid": "U1", "date": "2020-01-01T12:00:00", "original_filename": "confirmed.jpg"},
        {"uuid": "U2", "date": "2020-06-01T12:00:00", "original_filename": "review.jpg"},
    ])
    exif_json = json.dumps([
        {"SourceFile": "confirmed.jpg", "DateTimeOriginal": "2020:01:01 12:00:00"},
        {"SourceFile": "review.jpg", "DateTimeOriginal": "2020:06:01 12:01:00"},  # 60s away -> review
        {"SourceFile": "missing.jpg", "DateTimeOriginal": "2015:01:01 00:00:00"},
        {"SourceFile": "no_ts.jpg"},
    ])

    calls = []

    def fake_runner(cmd, cwd):
        calls.append(cmd)
        if cmd[1] == "query":
            return library_json, 0
        return exif_json, 0

    summary = photos_diff.run(
        dest, dry_run=False, out_dir=out_dir,
        osxphotos_bin="osxphotos", exiftool_bin="exiftool",
        process_runner=fake_runner,
        which_fn=lambda name: f"/usr/bin/{name}",
        platform_fn=lambda: "darwin",
    )

    assert summary.counts["confirmed"] == 1
    assert summary.counts["needs_review"] == 1
    assert summary.counts["truly_missing"] == 1
    assert summary.counts["no_exif_timestamp"] == 1

    assert (out_dir / "truly_missing.txt").read_text().strip() == "missing.jpg"
    assert (out_dir / "no_exif_timestamp.txt").read_text().strip() == "no_ts.jpg"
    assert "confirmed.jpg\tU1\t0" in (out_dir / "confirmed.txt").read_text()
    assert "review.jpg\tU2\t" in (out_dir / "needs_review.txt").read_text()
    assert not (out_dir / "_photos_diff_argfile.txt").exists()


def test_run_dry_run_computes_but_does_not_write_output_files(tmp_path):
    dest = tmp_path / "dest"
    _touch(dest / "a.jpg")
    out_dir = tmp_path / "out"

    library_json = json.dumps([{"uuid": "U1", "date": "2020-01-01T12:00:00", "original_filename": "a.jpg"}])
    exif_json = json.dumps([{"SourceFile": "a.jpg", "DateTimeOriginal": "2020:01:01 12:00:00"}])

    def fake_runner(cmd, cwd):
        return (library_json, 0) if cmd[1] == "query" else (exif_json, 0)

    summary = photos_diff.run(
        dest, dry_run=True, out_dir=out_dir,
        process_runner=fake_runner,
        which_fn=lambda name: f"/usr/bin/{name}",
        platform_fn=lambda: "darwin",
    )

    assert summary.counts["confirmed"] == 1  # still computed for real
    assert not out_dir.exists()  # nothing written


def test_run_preflight_fails_on_non_macos(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    summary = photos_diff.run(
        dest, dry_run=True, out_dir=tmp_path / "out",
        which_fn=lambda name: f"/usr/bin/{name}",
        platform_fn=lambda: "linux",
    )
    assert summary.counts["preflight_failed"] == 1


def test_run_no_media_files_short_circuits(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    calls = []
    summary = photos_diff.run(
        dest, dry_run=True, out_dir=tmp_path / "out",
        process_runner=lambda cmd, cwd: calls.append(cmd) or ("[]", 0),
        which_fn=lambda name: f"/usr/bin/{name}",
        platform_fn=lambda: "darwin",
    )
    assert calls == []
    assert summary.counts["source_files"] == 0
