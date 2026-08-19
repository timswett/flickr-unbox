"""
Tests for flickr_unbox.rename.

The stale-plan pre-flight check (new behavior vs. rename.sh, see
rename.py's docstring) is the main thing worth testing here beyond a
straight port -- it should refuse to run at all if dest's contents don't
match what the loaded plan describes. The per-row MISSING SOURCE /
target-collision defense-in-depth checks are tested by calling
execute_plan() directly against a hand-built rows list, since a plan that
passes the stale-plan check by construction can't normally hit those
branches (that's the point of the stale-plan check).
"""
from pathlib import Path

from flickr_unbox import rename
from flickr_unbox.rename_plan import PlanRow, build_plan, write_plan
from flickr_unbox._report import RunSummary


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_dest_and_plan(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "photo_111.json", "{}")
    _touch(dest / "111_secret_o.jpg", "img")
    plan_path = tmp_path / "plan.tsv"
    write_plan(build_plan(dest), plan_path)
    return dest, plan_path


def test_dry_run_renames_nothing(tmp_path):
    dest, plan_path = _make_dest_and_plan(tmp_path)

    summary = rename.run(dest, plan_path, dry_run=True)

    assert summary.counts["would_rename"] == 2
    assert (dest / "111_secret_o.jpg").exists()
    assert not (dest / "111.jpg").exists()


def test_no_dry_run_renames_ok_rows(tmp_path):
    dest, plan_path = _make_dest_and_plan(tmp_path)

    summary = rename.run(dest, plan_path, dry_run=False)

    assert summary.counts["renamed"] == 2
    assert (dest / "111.json").exists()
    assert (dest / "111.jpg").exists()
    assert not (dest / "photo_111.json").exists()


def test_non_ok_rows_are_skipped(tmp_path):
    dest = tmp_path / "alldata"
    _touch(dest / "no_id_here.jpg", "img")  # -> UNRESOLVED
    plan_path = tmp_path / "plan.tsv"
    write_plan(build_plan(dest), plan_path)

    summary = rename.run(dest, plan_path, dry_run=False)

    assert summary.counts["skipped_unresolved"] == 1
    assert (dest / "no_id_here.jpg").exists()  # left alone


def test_preflight_fails_when_plan_file_missing(tmp_path):
    dest = tmp_path / "alldata"
    dest.mkdir()
    summary = rename.run(dest, tmp_path / "does_not_exist.tsv", dry_run=True)
    assert summary.counts["preflight_failed"] == 1


def test_preflight_fails_on_stale_plan_new_file_added(tmp_path):
    dest, plan_path = _make_dest_and_plan(tmp_path)
    _touch(dest / "photo_222.json", "{}")  # dest changed after the plan was written

    summary = rename.run(dest, plan_path, dry_run=True)

    assert summary.counts["preflight_failed"] == 1
    assert not (dest / "111.json").exists()  # nothing renamed -- whole run refused


def test_preflight_fails_on_stale_plan_file_removed(tmp_path):
    dest, plan_path = _make_dest_and_plan(tmp_path)
    (dest / "111_secret_o.jpg").unlink()  # dest changed after the plan was written

    summary = rename.run(dest, plan_path, dry_run=True)

    assert summary.counts["preflight_failed"] == 1


def test_preflight_passes_when_dest_matches_plan_exactly(tmp_path):
    dest, plan_path = _make_dest_and_plan(tmp_path)
    result = rename.preflight(dest, plan_path)
    assert result.ok


def test_execute_plan_skips_missing_source(tmp_path):
    dest = tmp_path
    rows = [PlanRow("OK", "gone.jpg", "111.jpg")]
    summary = RunSummary(stage="rename", dry_run=False)

    rename.execute_plan(dest, rows, dry_run=False, summary=summary)

    assert summary.counts["missing_source"] == 1
    assert "renamed" not in summary.counts


def test_execute_plan_skips_target_collision(tmp_path):
    dest = tmp_path
    _touch(dest / "src.jpg", "src-content")
    _touch(dest / "111.jpg", "already-here")
    rows = [PlanRow("OK", "src.jpg", "111.jpg")]
    summary = RunSummary(stage="rename", dry_run=False)

    rename.execute_plan(dest, rows, dry_run=False, summary=summary)

    assert summary.counts["collision_at_move_time"] == 1
    assert (dest / "111.jpg").read_text() == "already-here"  # not overwritten
    assert (dest / "src.jpg").exists()  # not moved
