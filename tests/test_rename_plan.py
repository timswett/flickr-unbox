"""
Tests for flickr_unbox.rename_plan.

Unlike flatten/merge_photoinfo, this stage already has full fixture
coverage from the bash/perl era: test_data/alldata_sim/ + a hand-verified
expected_rename_plan.tsv (see generate_test_fixtures.py). The main test
here is a parity check against that existing golden master, not a
from-scratch fixture build.

Row order note: expected_rename_plan.tsv's rows are written in the
synthetic-case definition order in generate_test_fixtures.py, not derived
from actually running the real build_rename_plan.pl and capturing its
(differently-ordered, see rename_plan.py's docstring) raw output. So the
parity check compares rows as a set of (status, old, new) tuples, not
positionally -- order was never part of the contract being verified.
"""
import csv
from pathlib import Path

import pytest

from flickr_unbox import rename_plan

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLDATA_SIM = REPO_ROOT / "test_data" / "alldata_sim"
EXPECTED_TSV = REPO_ROOT / "test_data" / "expected_rename_plan.tsv"


def _load_expected():
    with open(EXPECTED_TSV, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return {(row["status"], row["old_name"], row["new_name"]) for row in reader}


@pytest.mark.skipif(not ALLDATA_SIM.is_dir(), reason="run generate_test_fixtures.py first")
def test_matches_golden_master_fixture():
    rows = rename_plan.build_plan(ALLDATA_SIM)
    actual = {(r.status, r.old, r.new) for r in rows}
    expected = _load_expected()
    assert actual == expected


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_ambiguous_double_hit_last_segment_wins(tmp_path):
    # Both underscore segments are numeric AND both have a real sidecar --
    # TODO #3: candidates are checked [last, second-to-last], last wins.
    _touch(tmp_path / "111_222_o.jpg")
    _touch(tmp_path / "photo_111.json")
    _touch(tmp_path / "photo_222.json")

    rows = {r.old: r for r in rename_plan.build_plan(tmp_path)}

    assert rows["111_222_o.jpg"].status == "OK"
    assert rows["111_222_o.jpg"].new == "222.jpg"  # last segment (222) wins


def test_collision_marks_both_sides(tmp_path):
    _touch(tmp_path / "111_secretaaaa_o.jpg")
    _touch(tmp_path / "111_secretbbbb_o.jpg")
    _touch(tmp_path / "photo_111.json")

    rows = {r.old: r for r in rename_plan.build_plan(tmp_path)}

    assert rows["111_secretaaaa_o.jpg"].status == "COLLISION"
    assert rows["111_secretbbbb_o.jpg"].status == "COLLISION"
    assert rows["photo_111.json"].status == "OK"


def test_unresolved_when_no_id_candidate_matches(tmp_path):
    _touch(tmp_path / "no_id_here.jpg")

    rows = {r.old: r for r in rename_plan.build_plan(tmp_path)}

    assert rows["no_id_here.jpg"].status == "UNRESOLVED"
    assert rows["no_id_here.jpg"].new == ""


def test_account_level_json_is_skipped_entirely(tmp_path):
    _touch(tmp_path / "albums.json")
    _touch(tmp_path / "photo_111.json")
    _touch(tmp_path / "111_secret_o.jpg")

    rows = {r.old: r for r in rename_plan.build_plan(tmp_path)}

    assert "albums.json" not in rows
    assert len(rows) == 2


def test_dry_run_does_not_write_plan_file(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "photo_111.json")
    plan_path = tmp_path / "plan.tsv"

    summary = rename_plan.run(dest, plan_path, dry_run=True)

    assert not plan_path.exists()
    assert summary.counts["ok"] == 1


def test_no_dry_run_writes_plan_file_with_header(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "photo_111.json")
    plan_path = tmp_path / "plan.tsv"

    rename_plan.run(dest, plan_path, dry_run=False)

    with open(plan_path) as f:
        lines = f.read().splitlines()
    assert lines[0] == "status\told_name\tnew_name"
    assert lines[1] == "OK\tphoto_111.json\t111.json"
