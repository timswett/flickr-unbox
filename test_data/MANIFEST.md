# Test Fixture Manifest

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
| 7 | `4700000007_secret{aaaa,bbbb}_o.jpg` | Genuine collision -- two files resolve to same target name |
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
(`perl -pi.bak -e 's/("(longitude|latitude)": ".*)(\d{6})/\1.\3/'`),
gated by a grep guard in `gps_fix.sh` that only matches purely-digit values.
Three cases: needs the fix, no GPS data (no-op), and already-decimal (no-op
-- see the code comment in this script for why the raw substitution is
NOT idempotent on its own and the guard is load-bearing).
