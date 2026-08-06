#!/usr/bin/env python3
"""
generate_test_fixtures.py

Generates a small, synthetic, license-free test fixture that mimics a
post-merge Flickr export (photo/video files + photo_<id>.json sidecars
living together in one flat folder, matching the real 'alldata' state
right before the rename step). No real personal photos or metadata are
used anywhere -- safe to commit to a public repo.

Every case below was chosen to reproduce a specific edge case discovered
while building the original bash/perl pipeline against a real ~100K-file
Flickr export. The matching/rename logic mirrored here is taken directly
from build_rename_plan.pl, so this fixture doubles as a regression oracle
for a Python port of that logic.

Output:
  <output_dir>/alldata_sim/          -- the synthetic flat folder
  <output_dir>/expected_rename_plan.tsv   -- hand-verified ground truth
  <output_dir>/junk_sample/          -- junk files + manifest, for testing
                                         the junk-sweep step in isolation
  <output_dir>/gps_fix_samples/      -- before/after pairs for the GPS
                                         decimal-point fix, tested separately
  <output_dir>/MANIFEST.md           -- human-readable description of
                                         every case and what it's testing

Usage:
  python3 generate_test_fixtures.py [output_dir]
  (default output_dir: ./test_data)

Requires Pillow to generate real, valid JPEG bytes (pip install pillow).
Falls back to a hardcoded minimal JPEG if Pillow isn't available -- fine
for filename/matching tests, but install Pillow if you want the exiftool
write-back tests to have fully typical files.

Uses ffmpeg (if found on PATH) to generate a real minimal .mov for the
video test case. Without ffmpeg, the video file is a placeholder --
good enough for filename-matching tests, not for exiftool write tests.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# A fallback minimal-but-valid JPEG (1x1 pixel), used only if Pillow isn't
# installed. Real cameras/exports never produce files this minimal, but
# it decodes fine and exiftool can write EXIF/APP1 segments into it.
FALLBACK_JPEG_HEX = (
    "ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303"
    "030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d0e11"
    "0e0b0b1016101113141515150c0f171816141812141514ffdb00430103040405"
    "0405090505091405050505050505050505050505050505050505050505050505"
    "0505050505050505050505050505050505050505050505050505ffc0001108000"
    "100010301220002110103110111ffc4001f0000010501010101010100000000"
    "000000000102030405060708090a0bffc400b5100002010303020403050504040"
    "000017d01020300041105122131410613516107227114328191a1082342b1c115"
    "52d1f02433627282090a161718191a25262728292a3435363738393a434445464"
    "748494a535455565758595a636465666768696a737475767778797a838485868"
    "788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3"
    "c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f"
    "7f8f9faffda0008010100003f00fb2800ffd9"
)


def make_jpeg(path: Path):
    try:
        from PIL import Image
        img = Image.new("RGB", (4, 4), color=(120, 140, 160))
        img.save(path, "JPEG")
    except ImportError:
        path.write_bytes(bytes.fromhex(FALLBACK_JPEG_HEX))
        print(f"  (Pillow not installed -- wrote fallback minimal JPEG for {path.name})")


def make_mov(path: Path):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=gray:s=32x32:d=1",
             "-loglevel", "quiet", str(path)],
            check=True,
        )
    else:
        path.write_bytes(b"NOT A REAL MOV - placeholder, ffmpeg not found on PATH")
        print(f"  (ffmpeg not found -- wrote placeholder for {path.name}, "
              f"fine for matching tests only)")


def flickr_json(name="", description="", date_taken="2018-06-15 14:22:03",
                 lat=None, lon=None, acc=None, tags=None, include_date=True,
                 albums=None):
    """Builds a JSON body matching the real Flickr export schema Tim
    reverse-engineered via `exiftool -s` on real sample files."""
    data = {
        "id": "",
        "name": name,
        "description": description,
    }
    if include_date:
        data["date_taken"] = date_taken
    geo = []
    if lat is not None:
        geo = [{
            "latitude": lat,
            "longitude": lon,
            "accuracy": acc if acc is not None else "16",
        }]
    data["geo"] = geo
    tags = tags or []
    data["tags"] = [{"tag": t} for t in tags]
    data["count_tags"] = str(len(tags))
    if albums:
        data["albums"] = albums
    return data


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2))


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_data")
    if out_dir.exists():
        shutil.rmtree(out_dir)

    alldata = out_dir / "alldata_sim"
    alldata.mkdir(parents=True)

    expected_rows = []  # (status, old_name, new_name)

    print("Generating alldata_sim/ cases...")

    # --- Case 1: standard <id>_<secret>_o.jpg shape, full metadata ---
    make_jpeg(alldata / "4700000001_a1b2c3d4e5_o.jpg")
    write_json(alldata / "photo_4700000001.json", flickr_json(
        name="Harbor at dusk",
        description="Taken from the pier",
        date_taken="2017-08-02 19:44:10",
        lat="12345678", lon="-87654321", acc="16",
        tags=["harbor", "dusk", "boston"],
        albums=[{"title": "New England Trip", "id": "72157600000001", "url": "https://flickr.com/albums/1"}],
    ))
    expected_rows.append(("OK", "4700000001_a1b2c3d4e5_o.jpg", "4700000001.jpg"))
    expected_rows.append(("OK", "photo_4700000001.json", "4700000001.json"))

    # --- Case 2: video_<id>.mov shape, minimal metadata, no date embedded ---
    make_mov(alldata / "video_4700000002.mov")
    write_json(alldata / "photo_4700000002.json", flickr_json(
        name="", description="", date_taken="2019-01-01 00:00:00",
    ))
    expected_rows.append(("OK", "video_4700000002.mov", "4700000002.mov"))
    expected_rows.append(("OK", "photo_4700000002.json", "4700000002.json"))

    # --- Case 3: <slug>_<id>_o.jpg, no secret, no GPS, has tags ---
    make_jpeg(alldata / "sunset-over-the-bay_4700000003_o.jpg")
    write_json(alldata / "photo_4700000003.json", flickr_json(
        name="Sunset over the bay",
        tags=["sunset", "bay"],
    ))
    expected_rows.append(("OK", "sunset-over-the-bay_4700000003_o.jpg", "4700000003.jpg"))
    expected_rows.append(("OK", "photo_4700000003.json", "4700000003.json"))

    # --- Case 4: <basefilename>_<id>_o.jpg, no secret, GPS present, no tags ---
    make_jpeg(alldata / "IMG_1234_4700000004_o.jpg")
    write_json(alldata / "photo_4700000004.json", flickr_json(
        name="IMG_1234",
        lat="45123456", lon="-93654321", acc="11",
    ))
    expected_rows.append(("OK", "IMG_1234_4700000004_o.jpg", "4700000004.jpg"))
    expected_rows.append(("OK", "photo_4700000004.json", "4700000004.json"))

    # --- Case 5: date_taken key entirely missing from JSON ---
    make_jpeg(alldata / "4700000005_f9e8d7c6b5_o.jpg")
    write_json(alldata / "photo_4700000005.json", flickr_json(
        name="No date on this one", description="Old scan", include_date=False,
    ))
    expected_rows.append(("OK", "4700000005_f9e8d7c6b5_o.jpg", "4700000005.jpg"))
    expected_rows.append(("OK", "photo_4700000005.json", "4700000005.json"))

    # --- Case 6: orphan JSON, no matching photo (simulates pre-dedup removal) ---
    write_json(alldata / "photo_4700000006.json", flickr_json(name="Orphaned sidecar"))
    expected_rows.append(("OK", "photo_4700000006.json", "4700000006.json"))
    # (no photo row for this id -- deliberately absent)

    # --- Case 7: genuine collision -- two different files resolve to the same id+ext ---
    make_jpeg(alldata / "4700000007_secretaaaa_o.jpg")
    make_jpeg(alldata / "4700000007_secretbbbb_o.jpg")
    write_json(alldata / "photo_4700000007.json", flickr_json(name="Duplicate export"))
    expected_rows.append(("COLLISION", "4700000007_secretaaaa_o.jpg", "4700000007.jpg"))
    expected_rows.append(("COLLISION", "4700000007_secretbbbb_o.jpg", "4700000007.jpg"))
    expected_rows.append(("OK", "photo_4700000007.json", "4700000007.json"))

    # --- Case 8: ambiguous numeric segments -- must fall back from last to
    # second-to-last segment because only one of them has a real JSON id ---
    make_jpeg(alldata / "4700000008_4700000099_o.jpg")
    write_json(alldata / "photo_4700000008.json", flickr_json(name="Disambiguated by JSON presence"))
    # NOTE: deliberately no photo_4700000099.json -- that's what forces the
    # matcher to fall back to the second-to-last underscore segment.
    expected_rows.append(("OK", "4700000008_4700000099_o.jpg", "4700000008.jpg"))
    expected_rows.append(("OK", "photo_4700000008.json", "4700000008.json"))

    # --- Case 9: unresolved -- no recognizable id pattern at all ---
    make_jpeg(alldata / "mystery_scan_no_id_here.jpg")
    expected_rows.append(("UNRESOLVED", "mystery_scan_no_id_here.jpg", ""))

    # --- Case 10: account-level JSON exports -- must be left completely alone ---
    write_json(alldata / "albums.json", {"albums": []})
    write_json(alldata / "contacts_part001.json", {"contacts": []})
    # (intentionally absent from expected_rows -- these must not appear at all)

    # --- Case 11: single-word slug_<id>_o shape -- short end of the length range ---
    make_jpeg(alldata / "surf_4700000011_o.jpg")
    write_json(alldata / "photo_4700000011.json", flickr_json(name="Surf"))
    expected_rows.append(("OK", "surf_4700000011_o.jpg", "4700000011.jpg"))
    expected_rows.append(("OK", "photo_4700000011.json", "4700000011.json"))

    # --- Case 12: long multi-word slug w/ leading hyphen -- mirrors a real
    # Flickr title-embedded filename seen in Tim's actual export (a long
    # hyphenated title, not the usual short id_secret shape). The matcher
    # is length-independent by construction (it only looks at the last
    # underscore-delimited token), so this locks that in as a regression
    # guard against a Python port that assumes a fixed-length prefix.
    make_jpeg(alldata / "-a-long-hyphenated-title-that-goes-on-for-a-while-before-the-id_4700000012_o.jpg")
    write_json(alldata / "photo_4700000012.json", flickr_json(name="Long title case"))
    expected_rows.append(("OK", "-a-long-hyphenated-title-that-goes-on-for-a-while-before-the-id_4700000012_o.jpg", "4700000012.jpg"))
    expected_rows.append(("OK", "photo_4700000012.json", "4700000012.json"))

    # --- Case 13: bare <id>_o.jpg -- no secret, no slug, zero underscores
    # before the id. Distinct from cases 3/4 (which both have a prefix
    # word), since here @parts has only one element after the split.
    make_jpeg(alldata / "4700000013_o.jpg")
    write_json(alldata / "photo_4700000013.json", flickr_json(name="Bare id, no secret"))
    expected_rows.append(("OK", "4700000013_o.jpg", "4700000013.jpg"))
    expected_rows.append(("OK", "photo_4700000013.json", "4700000013.json"))

    # --- Case 14: uppercase extension -- must normalize to lowercase in
    # the renamed output (`lc($1)` in the reference perl).
    make_jpeg(alldata / "4700000014_abc123_o.JPG")
    write_json(alldata / "photo_4700000014.json", flickr_json(name="Uppercase extension"))
    expected_rows.append(("OK", "4700000014_abc123_o.JPG", "4700000014.jpg"))
    expected_rows.append(("OK", "photo_4700000014.json", "4700000014.json"))

    # --- Case 15: video_<id> shape, uppercase prefix and extension --
    # reference regex is case-insensitive (`/^video_(\d+)\.(\w+)$/i`).
    make_mov(alldata / "VIDEO_4700000015.MOV")
    write_json(alldata / "photo_4700000015.json", flickr_json(name="Uppercase video prefix"))
    expected_rows.append(("OK", "VIDEO_4700000015.MOV", "4700000015.mov"))
    expected_rows.append(("OK", "photo_4700000015.json", "4700000015.json"))

    # --- Case 16: uppercase "_O" marker on the photo, uppercase "PHOTO_"
    # sidecar name -- reference regexes for both are case-insensitive.
    make_jpeg(alldata / "4700000016_abc123_O.JPG")
    write_json(alldata / "PHOTO_4700000016.JSON", flickr_json(name="Uppercase o-marker and sidecar name"))
    expected_rows.append(("OK", "4700000016_abc123_O.JPG", "4700000016.jpg"))
    expected_rows.append(("OK", "PHOTO_4700000016.JSON", "4700000016.json"))

    # --- Case 17: double-hit ambiguous -- BOTH underscore segments are
    # numeric AND both have a real JSON sidecar. The reference perl checks
    # parts[-1] before parts[-2] (candidates pushed in that order, first
    # grep hit wins), so the last segment silently wins with no ambiguity
    # flag raised. This locks in that exact tie-break behavior -- a Python
    # port that checked parts[-2] first, or flagged this as ambiguous,
    # would diverge from the validated original.
    make_jpeg(alldata / "4700000017_4700000018_o.jpg")
    write_json(alldata / "photo_4700000017.json", flickr_json(name="Also has a real id, but loses the tie-break"))
    write_json(alldata / "photo_4700000018.json", flickr_json(name="Wins the tie-break (last segment)"))
    expected_rows.append(("OK", "4700000017_4700000018_o.jpg", "4700000018.jpg"))
    expected_rows.append(("OK", "photo_4700000017.json", "4700000017.json"))
    expected_rows.append(("OK", "photo_4700000018.json", "4700000018.json"))

    # --- Case 18: both underscore segments look numeric, but NEITHER has
    # a matching JSON sidecar anywhere. Distinct UNRESOLVED path from case
    # 9 (no numeric pattern at all) -- this exercises the "candidates
    # exist but fail the json_ids{} verification" branch specifically.
    make_jpeg(alldata / "4700000019_4700000097_o.jpg")
    expected_rows.append(("UNRESOLVED", "4700000019_4700000097_o.jpg", ""))

    # --- Case 19: non-ASCII slug -- cheap insurance for the Windows-port
    # motivation behind the Python rewrite. The id itself is always plain
    # ASCII digits, so this should resolve fine in both Perl (byte-level
    # \w) and Python (unicode-aware \w by default) as long as the port
    # only inspects the trailing numeric segment. Also exercises reading/
    # writing a non-ASCII filename through pathlib.
    make_jpeg(alldata / "café-au-lait_4700000020_o.jpg")
    write_json(alldata / "photo_4700000020.json", flickr_json(name="Non-ASCII slug"))
    expected_rows.append(("OK", "café-au-lait_4700000020_o.jpg", "4700000020.jpg"))
    expected_rows.append(("OK", "photo_4700000020.json", "4700000020.json"))

    # Write ground truth
    plan_path = out_dir / "expected_rename_plan.tsv"
    with open(plan_path, "w") as f:
        f.write("status\told_name\tnew_name\n")
        for status, old, new in expected_rows:
            f.write(f"{status}\t{old}\t{new}\n")
    print(f"Wrote {len(expected_rows)} expected rows to {plan_path}")

    # --- Junk sample folder: tests the junk-sweep step in isolation ---
    junk_dir = out_dir / "junk_sample"
    junk_dir.mkdir()
    (junk_dir / "._4700000001_a1b2c3d4e5_o.jpg").write_bytes(b"applesingle-junk")
    (junk_dir / ".DS_Store").write_bytes(b"finder-junk")
    (junk_dir / "photo_4700000001.json.bak").write_text('{"leftover": "from perl -pi.bak"}')
    (junk_dir / "real_file_not_junk.jpg").write_bytes(b"keep-me")
    (junk_dir / "junk_manifest.txt").write_text(
        "These files should be deleted by a correct junk-sweep step:\n"
        "  ._4700000001_a1b2c3d4e5_o.jpg\n"
        "  .DS_Store\n"
        "  photo_4700000001.json.bak\n"
        "This file should survive the sweep:\n"
        "  real_file_not_junk.jpg\n"
    )
    print(f"Wrote junk_sample/ with manifest")

    # --- GPS fix samples: before/after pairs for the decimal-point fix ---
    # IMPORTANT: real Flickr JSON exports are pretty-printed, one key per
    # line (confirmed against a real sidecar: 78 lines, latitude/longitude
    # each on their own line) -- NOT a single compact line. This matters
    # because gps_fix.sh's perl substitution has no /g flag and relies on
    # perl -pi's line-by-line processing: on a real file, each of
    # latitude/longitude gets its own independent substitution pass. A
    # single-line JSON fixture would make the (line-spanning, greedy) regex
    # behave completely differently -- verified by testing a compact-JSON
    # version of this fixture, which only fixed the last field on the line
    # and left the other one untouched. Always build these as pretty JSON
    # (one key per line) to match the real format the regex is designed for.
    gps_dir = out_dir / "gps_fix_samples"
    gps_dir.mkdir()

    def write_pretty_geo_json(path: Path, lat: str, lon: str, acc: str = "16"):
        geo = [{"latitude": lat, "longitude": lon, "accuracy": acc}] if lat else []
        write_json(path, {"geo": geo})

    write_pretty_geo_json(gps_dir / "needs_fix_before.json", "12345678", "-87654321")
    write_pretty_geo_json(gps_dir / "needs_fix_after_expected.json", "12.345678", "-87.654321")
    write_pretty_geo_json(gps_dir / "no_gps_noop_before.json", "", "")
    write_pretty_geo_json(gps_dir / "no_gps_noop_after_expected.json", "", "")

    # Already-decimal values must be a no-op. This is NOT a trivial case:
    # the raw perl substitution `s/("(longitude|latitude)": ".*)(\d{6})/\1.\3/`
    # is NOT idempotent by itself -- run standalone against an
    # already-fixed value, it inserts a second decimal point (verified:
    # "12.345678" -> "12..345678"). What actually makes the real pipeline
    # safe is the *guard* in gps_fix.sh: `grep -qE '"(longitude|latitude)":
    # "-?[0-9]+"'` only matches when the value is purely digits immediately
    # followed by the closing quote -- a decimal point breaks that, so the
    # guard skips already-fixed files before perl ever touches them. A
    # Python port must replicate the guard+fix as a pair, not just the
    # substitution -- reimplementing "needs fixing" more loosely (e.g.
    # "no '.' anywhere in the value") would reintroduce the double-dot bug.
    write_pretty_geo_json(gps_dir / "already_decimal_noop_before.json", "12.345678", "-87.654321")
    write_pretty_geo_json(gps_dir / "already_decimal_noop_after_expected.json", "12.345678", "-87.654321")
    print("Wrote gps_fix_samples/ before/after pairs")

    # --- Manifest ---
    manifest = out_dir / "MANIFEST.md"
    manifest.write_text(f"""# Test Fixture Manifest

Synthetic data only -- no real photos or personal metadata. Safe to commit.

## alldata_sim/ (paired with expected_rename_plan.tsv)

| Case | File(s) | Tests |
|---|---|---|
| 1 | `4700000001_a1b2c3d4e5_o.jpg` | Standard id_secret_o shape, full metadata (GPS, tags, album) |
| 2 | `video_4700000002.mov` | video_<id> shape, minimal metadata, no GPS/tags |
| 3 | `sunset-over-the-bay_4700000003_o.jpg` | slug_<id>_o shape (no secret), no GPS |
| 4 | `IMG_1234_4700000004_o.jpg` | basefilename_<id>_o shape (no secret), GPS present |
| 5 | `4700000005_f9e8d7c6b5_o.jpg` | JSON missing `date_taken` key entirely |
| 6 | `photo_4700000006.json` (no photo) | Orphan JSON sidecar (simulates pre-dedup removal) |
| 7 | `4700000007_secret{{aaaa,bbbb}}_o.jpg` | Genuine collision -- two files resolve to same target name |
| 8 | `4700000008_4700000099_o.jpg` | Ambiguous numeric segments, resolved by checking which id has a real JSON |
| 9 | `mystery_scan_no_id_here.jpg` | Unresolvable filename -- no id pattern matches |
| 10 | `albums.json`, `contacts_part001.json` | Account-level JSON that must be left alone, not renamed |
| 11 | `surf_4700000011_o.jpg` | slug_<id>_o shape, short end of length range |
| 12 | `-a-long-hyphenated-title-..._4700000012_o.jpg` | slug_<id>_o shape, long end of length range (mirrors a real title-embedded filename) |
| 13 | `4700000013_o.jpg` | Bare id_o shape -- no secret, no slug, zero underscores before the id |
| 14 | `4700000014_abc123_o.JPG` | Uppercase extension must normalize to lowercase in output |
| 15 | `VIDEO_4700000015.MOV` | video_<id> prefix/extension case-insensitivity |
| 16 | `4700000016_abc123_O.JPG`, `PHOTO_4700000016.JSON` | Case-insensitive `_o` marker and `photo_` sidecar prefix |
| 17 | `4700000017_4700000018_o.jpg` | Double-hit ambiguous -- both segments numeric AND both have real JSON; last segment wins the tie-break |
| 18 | `4700000019_4700000097_o.jpg` | Both segments numeric but neither has a JSON -- distinct UNRESOLVED path from case 9 |
| 19 | `café-au-lait_4700000020_o.jpg` | Non-ASCII slug -- Windows-port/unicode-handling insurance |

## junk_sample/

AppleDouble (`._*`), `.DS_Store`, and a leftover `.bak` file, plus one real
file that must survive. See `junk_manifest.txt` for exact expected deletions.

## gps_fix_samples/

Before/after pairs for the GPS decimal-point fix
(`perl -pi.bak -e 's/("(longitude|latitude)": ".*)(\\d{{6}})/\\1.\\3/'`),
gated by a grep guard in `gps_fix.sh` that only matches purely-digit values.
Three cases: needs the fix, no GPS data (no-op), and already-decimal (no-op
-- see the code comment in this script for why the raw substitution is
NOT idempotent on its own and the guard is load-bearing).
""")
    print(f"Wrote {manifest}")

    print("\nDone. Fixture written to:", out_dir.resolve())


if __name__ == "__main__":
    main()
