"""
Tests for flickr_unbox.cleanup.

Pure filesystem bookkeeping -- no real exiftool or real data needed here,
the receipt format is already validated by exif_write.py's own tests and
real-data pass. Focus is the three distinct gate-refusal reasons (missing
receipt, errors present, stale count mismatch), --force bypassing only
those, and the removed/missing counting itself.
"""
import json
from pathlib import Path

from flickr_unbox import cleanup


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_batch_file(path: Path, names) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(names) + "\n")


def _write_receipt(batch_dir: Path, batch_num: str, files_updated: int, files_errored: int = 0) -> None:
    payload = {
        "batch_num": batch_num,
        "files_updated": files_updated,
        "files_errored": files_errored,
        "warnings": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    (batch_dir / f"batch_{batch_num}.result.json").write_text(json.dumps(payload))


def _setup(tmp_path, names, with_originals=True):
    dest = tmp_path / "alldata"
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", names)
    for name in names:
        _touch(dest / name)
        if with_originals:
            _touch(dest / f"{name}_original")
    return dest, batch_dir


# --- the three distinct gate-refusal reasons ---

def test_refuses_when_receipt_missing(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg"])
    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("no result receipt found" in n for n in summary.notes)
    assert (dest / "1.jpg_original").exists()  # untouched


def test_refuses_when_receipt_has_errors(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg"])
    _write_receipt(batch_dir, "01", files_updated=0, files_errored=1)
    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("reported 1 error" in n for n in summary.notes)


def test_refuses_when_receipt_is_stale(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg", "2.jpg"])
    _write_receipt(batch_dir, "01", files_updated=1, files_errored=0)  # batch now has 2 files
    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("looks stale" in n for n in summary.notes)


def test_refuses_when_receipt_is_unparseable(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg"])
    (batch_dir / "batch_01.result.json").write_text("{not json")
    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("could not be parsed" in n for n in summary.notes)


def test_passes_when_receipt_is_clean_and_matches(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg", "2.jpg"])
    _write_receipt(batch_dir, "01", files_updated=2, files_errored=0)
    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert "preflight_failed" not in summary.counts
    assert summary.counts["would_remove"] == 2


def test_refuses_when_receipt_missing_required_keys(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg"])
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "batch_01.result.json").write_text(json.dumps({"batch_num": "01"}))
    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("missing required field" in n for n in summary.notes)


def test_refuses_when_receipt_batch_num_mismatches(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg"])
    _write_receipt(batch_dir, "01", files_updated=1, files_errored=0)
    receipt_path = batch_dir / "batch_01.result.json"
    payload = json.loads(receipt_path.read_text())
    payload["batch_num"] = "02"  # simulates a receipt copied/misplaced from a different batch
    receipt_path.write_text(json.dumps(payload))

    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("is for batch '02', not batch '01'" in n for n in summary.notes)


def test_passes_when_receipt_accounts_for_files_unchanged(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg", "2.jpg"])
    payload = {
        "batch_num": "01",
        "files_updated": 1,
        "files_unchanged": 1,
        "files_errored": 0,
        "warnings": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    (batch_dir / "batch_01.result.json").write_text(json.dumps(payload))

    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)
    assert "preflight_failed" not in summary.counts
    assert summary.counts["would_remove"] == 2


# --- --force bypasses the gate, not the structural checks ---

def test_force_bypasses_missing_receipt(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg"])
    summary = cleanup.run(dest, batch_dir, "01", dry_run=False, force=True)
    assert "preflight_failed" not in summary.counts
    assert summary.counts["removed"] == 1
    assert any("--force" in n for n in summary.notes)


def test_force_bypasses_errors_present(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg"])
    _write_receipt(batch_dir, "01", files_updated=0, files_errored=1)
    summary = cleanup.run(dest, batch_dir, "01", dry_run=False, force=True)
    assert "preflight_failed" not in summary.counts
    assert summary.counts["removed"] == 1


def test_force_does_not_bypass_missing_batch_file(tmp_path):
    dest = tmp_path / "alldata"
    dest.mkdir()
    batch_dir = tmp_path / "batches"
    summary = cleanup.run(dest, batch_dir, "01", dry_run=False, force=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("batch file not found" in n for n in summary.notes)


def test_force_does_not_bypass_missing_dest(tmp_path):
    batch_dir = tmp_path / "batches"
    _write_batch_file(batch_dir / "batch_01.txt", ["1.jpg"])
    summary = cleanup.run(tmp_path / "does_not_exist", batch_dir, "01", dry_run=False, force=True)
    assert summary.counts["preflight_failed"] == 1
    assert any("dest does not exist" in n for n in summary.notes)


# --- removed/missing counting, and dry-run vs real ---

def test_dry_run_does_not_delete_originals(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg", "2.jpg"])
    _write_receipt(batch_dir, "01", files_updated=2, files_errored=0)

    summary = cleanup.run(dest, batch_dir, "01", dry_run=True)

    assert summary.counts["would_remove"] == 2
    assert (dest / "1.jpg_original").exists()
    assert (dest / "2.jpg_original").exists()


def test_no_dry_run_deletes_originals(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg", "2.jpg"])
    _write_receipt(batch_dir, "01", files_updated=2, files_errored=0)

    summary = cleanup.run(dest, batch_dir, "01", dry_run=False)

    assert summary.counts["removed"] == 2
    assert not (dest / "1.jpg_original").exists()
    assert not (dest / "2.jpg_original").exists()
    assert (dest / "1.jpg").exists()  # the real file itself is untouched


def test_missing_originals_are_counted_not_errors(tmp_path):
    dest, batch_dir = _setup(tmp_path, ["1.jpg", "2.jpg"], with_originals=False)
    _write_receipt(batch_dir, "01", files_updated=2, files_errored=0)

    summary = cleanup.run(dest, batch_dir, "01", dry_run=False)

    assert summary.counts["removed"] == 0
    assert summary.counts["already_clean_or_never_created"] == 2


def test_unlink_failure_is_logged_and_does_not_crash_the_run(tmp_path, monkeypatch):
    dest, batch_dir = _setup(tmp_path, ["1.jpg", "2.jpg"])
    _write_receipt(batch_dir, "01", files_updated=2, files_errored=0)

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "1.jpg_original":
            raise OSError("permission denied (simulated)")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    summary = cleanup.run(dest, batch_dir, "01", dry_run=False)

    assert summary.counts["removed"] == 1
    assert summary.counts["failed"] == 1
    assert any("FAILED" in n and "1.jpg_original" in n for n in summary.notes)
    assert (dest / "1.jpg_original").exists()  # unlink failed -- left in place
    assert not (dest / "2.jpg_original").exists()  # the other file still got cleaned up
