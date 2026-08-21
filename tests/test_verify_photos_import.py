"""
Tests for flickr_unbox.verify_photos_import.

Pure text processing -- no subprocess, no osxphotos, no real Photos
library needed. Log-line fixtures here are a faithful translation of
verify_import_completeness.sh's grep/awk/sed patterns rather than a real
captured import log (none exists in this repo -- see the module
docstring).
"""
from flickr_unbox import verify_photos_import


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- load_intended() ---

def test_load_intended_takes_basename_of_full_paths(tmp_path):
    p = tmp_path / "intended.txt"
    _write(p, "/Volumes/EAGET/alldata/1.jpg\n2.jpg\n\n1.jpg\n")
    assert verify_photos_import.load_intended(p) == {"1.jpg", "2.jpg"}


# --- parse_log(): ANSI stripping + each outcome category ---

def test_parse_log_strips_ansi_before_matching():
    from flickr_unbox import _osxphotos

    raw = "\x1b[32mAdded\x1b[0m 1.jpg to album Flickr Import\n"
    clean = _osxphotos.strip_ansi(raw)
    outcomes = verify_photos_import.parse_log(clean)
    assert outcomes.added == {"1.jpg"}


def test_parse_log_added_and_imported_and_orphans():
    text = (
        "Imported 1.jpg with UUID AAA\n"
        "Added 1.jpg to album Flickr Import\n"
        "Imported 2.jpg with UUID BBB\n"  # never added -- a crash mid-file
    )
    outcomes = verify_photos_import.parse_log(text)
    assert outcomes.added == {"1.jpg"}
    assert outcomes.imported == {"1.jpg", "2.jpg"}
    assert outcomes.orphans == {"2.jpg"}


def test_parse_log_error_importing_file():
    text = "some prefix text Error importing file 3.jpg\n"
    outcomes = verify_photos_import.parse_log(text)
    assert outcomes.errors == {"3.jpg"}


def test_parse_log_dupskipped_third_whitespace_field():
    # Mirrors awk '{print $3}' against a "^Skipping duplicate" line -- the
    # 3rd whitespace-separated token. Assumed line shape "Skipping
    # duplicate <filename> ..." (tokens 1-2 are literally "Skipping
    # duplicate", token 3 is the filename) since no real sample exists in
    # this repo -- see module docstring.
    text = "Skipping duplicate 4.jpg (already in library)\n"
    outcomes = verify_photos_import.parse_log(text)
    assert outcomes.dupskipped == {"4.jpg"}


def test_parse_log_ignores_non_matching_lines():
    text = "some random log noise\nanother line\n"
    outcomes = verify_photos_import.parse_log(text)
    assert outcomes.added == set()
    assert outcomes.imported == set()
    assert outcomes.errors == set()
    assert outcomes.dupskipped == set()


# --- LogOutcomes.orphans / accounted_for ---

def test_orphans_is_imported_minus_added():
    outcomes = verify_photos_import.LogOutcomes(
        added={"a.jpg"}, imported={"a.jpg", "b.jpg"}, errors=set(), dupskipped=set()
    )
    assert outcomes.orphans == {"b.jpg"}


def test_accounted_for_is_union_of_all_four_buckets():
    outcomes = verify_photos_import.LogOutcomes(
        added={"a.jpg"}, imported={"a.jpg", "b.jpg"}, errors={"c.jpg"}, dupskipped={"d.jpg"}
    )
    assert outcomes.accounted_for == {"a.jpg", "b.jpg", "c.jpg", "d.jpg"}


# --- run(): end to end ---

def test_run_end_to_end_writes_all_six_files_and_summary(tmp_path):
    intended = tmp_path / "intended.txt"
    _write(intended, "a.jpg\nb.jpg\nc.jpg\nd.jpg\ne.jpg\n")

    log = tmp_path / "import.log"
    _write(
        log,
        "Imported a.jpg with UUID AAA\n"
        "Added a.jpg to album X\n"
        "Imported b.jpg with UUID BBB\n"  # orphan
        "Error importing file c.jpg\n"
        "Skipping duplicate d.jpg (already in library)\n"
        # e.jpg: nothing logged at all -- genuinely missing
    )

    out_dir = tmp_path / "out"
    summary = verify_photos_import.run(intended, log, out_dir)

    assert summary.counts["intended"] == 5
    assert summary.counts["added"] == 1
    assert summary.counts["orphans"] == 1
    assert summary.counts["errors"] == 1
    assert summary.counts["dupskipped"] == 1
    assert summary.counts["missing"] == 1

    assert (out_dir / "missing.txt").read_text().strip() == "e.jpg"
    assert (out_dir / "added.txt").read_text().strip() == "a.jpg"
    assert (out_dir / "orphans.txt").read_text().strip() == "b.jpg"
    assert (out_dir / "errors.txt").read_text().strip() == "c.jpg"
    assert (out_dir / "dupskipped.txt").read_text().strip() == "d.jpg"
    assert sorted((out_dir / "intended.txt").read_text().split()) == ["a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"]


def test_run_no_missing_files_reconciles_exactly(tmp_path):
    intended = tmp_path / "intended.txt"
    _write(intended, "a.jpg\n")
    log = tmp_path / "import.log"
    _write(log, "Imported a.jpg with UUID AAA\nAdded a.jpg to album X\n")

    summary = verify_photos_import.run(intended, log, tmp_path / "out")
    assert summary.counts["missing"] == 0
    assert not any(n.startswith("MISSING files") for n in summary.notes)


def test_run_preflight_fails_when_intended_file_missing(tmp_path):
    summary = verify_photos_import.run(tmp_path / "nope.txt", tmp_path / "also_nope.log", tmp_path / "out")
    assert summary.counts["preflight_failed"] == 1


def test_run_preflight_fails_when_log_file_missing(tmp_path):
    intended = tmp_path / "intended.txt"
    _write(intended, "a.jpg\n")
    summary = verify_photos_import.run(intended, tmp_path / "nope.log", tmp_path / "out")
    assert summary.counts["preflight_failed"] == 1


def test_run_strips_ansi_from_the_real_log_file(tmp_path):
    intended = tmp_path / "intended.txt"
    _write(intended, "a.jpg\n")
    log = tmp_path / "import.log"
    _write(log, "\x1b[32mAdded\x1b[0m a.jpg to album X\n")

    summary = verify_photos_import.run(intended, log, tmp_path / "out")
    assert summary.counts["added"] == 1
    assert summary.counts["missing"] == 0
