"""
Tests for flickr_unbox.gps_fix.

Two sources of coverage:
  1. Parity against the 3 existing before/after fixture pairs in
     test_data/gps_fix_samples/ (needs_fix, already_decimal_noop,
     no_gps_noop) -- these predate this port and were hand-verified
     against the real bash/perl logic.
  2. New cases for TODO #1's two boundary cases (exactly-6-digit and
     fewer-than-6-digit values) plus the new anomaly-logging behavior,
     added inline here rather than as new files under test_data/, matching
     the precedent set in test_rename_plan.py for post-fixture edge cases.
"""
import json
from pathlib import Path

import pytest

from flickr_unbox import gps_fix

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "test_data" / "gps_fix_samples"


def _touch(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# --- Fixture parity (dict maps case name -> expected fields_fixed) ---

FIXTURE_CASES = {
    "needs_fix": 2,
    "already_decimal_noop": 0,
    "no_gps_noop": 0,
}


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="run generate_test_fixtures.py first")
@pytest.mark.parametrize("case,expected_fields_fixed", FIXTURE_CASES.items())
def test_matches_fixture_before_after_pair(case, expected_fields_fixed):
    before = (SAMPLES / f"{case}_before.json").read_text()
    after_expected = (SAMPLES / f"{case}_after_expected.json").read_text()

    result = gps_fix.fix_text(before)

    assert result.fields_fixed == expected_fields_fixed
    assert json.loads(result.text) == json.loads(after_expected)
    if expected_fields_fixed == 0:
        assert result.text == before  # untouched byte-for-byte, not just JSON-equal


# --- TODO #1 boundary cases ---

def test_exactly_six_digits_gets_leading_zero():
    # Bug in bash: "123456" -> ".123456" (no leading 0). Must be "0.123456".
    text = '{"geo": [{"latitude": "123456", "longitude": "-123456"}]}'
    result = gps_fix.fix_text(text)
    assert result.fields_fixed == 2
    assert '"latitude": "0.123456"' in result.text
    assert '"longitude": "-0.123456"' in result.text


def test_fewer_than_six_digits_is_fixed_not_silently_skipped():
    # Bug in bash: "1234" doesn't match \d{6}, so nothing happens at all.
    text = '{"geo": [{"latitude": "1234", "longitude": "-56"}]}'
    result = gps_fix.fix_text(text)
    assert result.fields_fixed == 2
    assert '"latitude": "0.001234"' in result.text
    assert '"longitude": "-0.000056"' in result.text


def test_large_value_uses_exact_string_math_not_float():
    # A value where naive float division (raw / 1e6) would show representation
    # error (e.g. 42366702/1e6 -> 42.36670199999999 in raw Python float repr).
    text = '{"geo": [{"latitude": "42366702", "longitude": "-110828138"}]}'
    result = gps_fix.fix_text(text)
    assert '"latitude": "42.366702"' in result.text
    assert '"longitude": "-110.828138"' in result.text


# --- Idempotency ---

def test_running_twice_is_a_noop_the_second_time():
    text = '{"geo": [{"latitude": "42366702", "longitude": "-110828138"}]}'
    once = gps_fix.fix_text(text)
    twice = gps_fix.fix_text(once.text)
    assert twice.fields_fixed == 0
    assert twice.text == once.text


# --- Anomalous values: new behavior vs. bash (logged, not silently ignored) ---

def test_anomalous_value_is_flagged_and_left_untouched():
    text = '{"geo": [{"latitude": "not-a-number", "longitude": "-110828138"}]}'
    result = gps_fix.fix_text(text)
    assert result.anomalies == ["not-a-number"]
    assert '"latitude": "not-a-number"' in result.text  # untouched
    assert '"longitude": "-110.828138"' in result.text  # sibling field still fixed


def test_empty_value_is_not_flagged_as_anomalous():
    text = '{"geo": [{"latitude": "", "longitude": ""}]}'
    result = gps_fix.fix_text(text)
    assert result.anomalies == []
    assert result.fields_fixed == 0


# --- run() / CLI-level behavior ---

def test_dry_run_touches_nothing_on_disk(tmp_path):
    dest = tmp_path
    _touch(dest / "111.json", '{"geo": [{"latitude": "42366702", "longitude": "-110828138"}]}')

    summary = gps_fix.run(dest, dry_run=True)

    assert summary.counts["files_to_fix"] == 1
    assert summary.counts["fields_to_fix"] == 2
    assert "42366702" in (dest / "111.json").read_text()  # untouched


def test_no_dry_run_writes_fixed_file(tmp_path):
    dest = tmp_path
    _touch(dest / "111.json", '{"geo": [{"latitude": "42366702", "longitude": "-110828138"}]}')

    summary = gps_fix.run(dest, dry_run=False)

    assert summary.counts["files_fixed"] == 1
    assert summary.counts["fields_fixed"] == 2
    assert "42.366702" in (dest / "111.json").read_text()


def test_only_numeric_id_json_files_are_considered(tmp_path):
    dest = tmp_path
    _touch(dest / "111.json", '{"geo": [{"latitude": "42366702", "longitude": "-110828138"}]}')
    _touch(dest / "albums.json", '{"latitude": "42366702"}')  # not a sidecar, must be ignored

    summary = gps_fix.run(dest, dry_run=True)

    assert summary.counts["sidecars_scanned"] == 1
    assert "42366702" in (dest / "albums.json").read_text()  # never touched


def test_preflight_fails_on_missing_dest(tmp_path):
    summary = gps_fix.run(tmp_path / "does_not_exist", dry_run=True)
    assert summary.counts["preflight_failed"] == 1


def test_warns_when_pre_rename_sidecars_are_present(tmp_path):
    # gps-fix only recognizes already-renamed "<id>.json" sidecars. Without
    # this check, running it before `rename` (or on files rename left
    # un-renamed due to a collision) silently scans 0 files with no hint of
    # why -- indistinguishable from a legitimate "nothing left to fix" run.
    dest = tmp_path
    _touch(dest / "photo_111.json", '{"geo": [{"latitude": "42366702", "longitude": "-110828138"}]}')

    summary = gps_fix.run(dest, dry_run=True)

    assert summary.counts["sidecars_scanned"] == 0
    assert summary.counts["pre_rename_sidecars_found"] == 1
    assert any("pre-rename" in n and "1 " in n for n in summary.notes)


def test_no_warning_when_only_renamed_sidecars_present(tmp_path):
    dest = tmp_path
    _touch(dest / "111.json", '{"geo": [{"latitude": "42366702", "longitude": "-110828138"}]}')

    summary = gps_fix.run(dest, dry_run=True)

    assert "pre_rename_sidecars_found" not in summary.counts


def test_sidecar_with_non_ascii_content_round_trips_correctly(tmp_path):
    dest = tmp_path
    _touch(dest / "111.json", '{"title": "café – Résumé", "geo": [{"latitude": "42366702", "longitude": "-110828138"}]}')

    summary = gps_fix.run(dest, dry_run=False)

    assert summary.counts["files_fixed"] == 1
    written = (dest / "111.json").read_text(encoding="utf-8")
    assert "café – Résumé" in written
    assert "42.366702" in written
