# flickr-unbox

Scripts to unbox a full Flickr data export: match photos/videos to their
separately-exported JSON metadata, restore real EXIF/IPTC (title,
description, date, GPS, tags) into the actual files, and do it at
100K-file scale without running your disk out of space.

This project implements (and extends) the method described in
[How to export Flickr photos with metadata attached](https://philipmallis.com/2023/09/11/how-to-export-flickr-photos-with-metadata-attached/)
by Philip Mallis — credit to that post and its comment thread for the
original approach and several of the exiftool gotchas below. This repo
exists because that method, done by hand, doesn't hold up past a few
hundred files; this is the automated, tested version.

## Why this exists

Flickr's full account export splits your photos/videos from their
metadata: images come with no useful EXIF, and a separate batch of
`photo_<id>.json` sidecar files hold the title, description, capture
date, GPS coordinates, and tags. Reuniting them requires solving a few
non-obvious problems:

- **Filename → ID matching is ambiguous.** Flickr filenames come in
  multiple shapes (`<id>_<secret>_o.ext`, `video_<id>.ext`,
  `<slug>_<id>_o.ext`, `<basefilename>_<id>_o.ext`), and a 10-digit
  "secret" can look identical to a photo ID by shape alone.
- **GPS coordinates are stored wrong.** Flickr's JSON has latitude/longitude
  as integers with no decimal point (e.g. `12345678` instead of
  `12.345678`).
- **Disk space.** exiftool's default backup behavior (`_original` files)
  doubles the space needed for every file it touches — at real library
  sizes (hundreds of GB), a single unbatched pass can run you out of
  space mid-run.

## Status

The core pipeline (8 stages, `src/flickr_unbox/`) is implemented in Python
and runnable via the `flickr-unbox` CLI, ported from a working bash/perl
pipeline that already processed a real ~100K-file / 300GB+ Flickr export
successfully. Every stage has been validated end to end against real data,
most recently a 2,321-file run through the full 7-stage CLI chain with the
real `exiftool` binary — zero errors. The Python port hasn't yet been run
against the full ~50K-file/300GB+ real library at scale (that's a
deliberately bigger, more careful step than anything validated so far),
and GitHub Actions CI isn't wired up yet.

The optional, macOS-only Apple Photos import tail (`photos-diff` through
`photos-fix-dates`) is a Python port of scripts that already processed a
real ~14K-file import in production, hardened through several real
incidents there (see README's "macOS: importing into Apple Photos"
section and each module's docstring) — `photos-diff` itself is new code
built from the same validated method, not a port of an existing script,
and hasn't yet been run against real data as this Python port (see
CLAUDE.md).

178 tests pass total (synthetic, hermetic, safe for CI, no real
osxphotos/Photos.app dependency even for the photos-* stages), and a
`bandit` security pass is clean.

## Disclaimer — use at your own risk

This tool renames files, rewrites EXIF/IPTC metadata in place, and
deletes backup files it creates along the way. **Run it against a copy
of your data, not your only copy**, and keep an independent backup
regardless of what this README or the tool's own output tells you.

A meaningful amount of work has gone into testing before any run that
touches real files — every subcommand defaults to a dry run that does
full validation and prints a summary without changing anything on disk;
`--no-dry-run` re-validates from current on-disk state rather than
trusting an earlier dry-run; `cleanup` refuses to delete `_original`
backups unless the matching `exif-write` run reported zero errors; and
the pipeline has been exercised against both a synthetic fixture suite
and real-data samples end-to-end with zero errors, most recently a
2,321-file run (see "Status" above). None of that amounts to a
guarantee. It hasn't been run against a full real library at scale, it
hasn't been tested on every OS/
filesystem/Flickr-export variant that exists, and no amount of
pre-flight checking eliminates the risk of a bug, an edge case, or a
mistake in how it's invoked.

This software is provided "as is," without warranty of any kind, and
the author accepts no liability for data loss, corruption, or any other
damage resulting from its use — see [LICENSE](LICENSE). By running it,
you accept that risk.

## Requirements

- **Python 3.9+** — no third-party pip dependencies for the pipeline
  itself (stdlib only; `pytest`/`pillow` are dev/testing-only extras).
- **[exiftool](https://exiftool.org/)** on your PATH — the one external
  prerequisite, needed only by the `exif-write` stage. Run
  `flickr-unbox doctor` after installing to confirm it's found (every
  other command also prints a one-line check of this automatically, so
  you'll see it either way before you get several stages into a
  workflow).
- Optional, macOS-only: importing into Apple Photos needs
  [osxphotos](https://github.com/RhetTbull/osxphotos) — see
  "macOS: importing into Apple Photos" below. Not needed for the core
  pipeline above.

## Installation

```
pip install -e .
flickr-unbox doctor
```

## Usage

One command per pipeline stage, run in order against a flattened Flickr
export directory. Every command defaults to a dry run — it validates and
reports what it would do without touching anything — and requires
`--no-dry-run` to actually act:

```
flickr-unbox flatten <source_base> <dest>
flickr-unbox merge-photoinfo <source_base> <dest>
flickr-unbox rename-plan <dest> --plan-path <plan.tsv>
flickr-unbox rename <dest> --plan-path <plan.tsv>
flickr-unbox gps-fix <dest>
flickr-unbox exif-batches <dest> --batch-dir <batches/>
flickr-unbox exif-write <dest> <batch_num> --batch-dir <batches/>
flickr-unbox cleanup <dest> <batch_num> --batch-dir <batches/>
```

Add `--no-dry-run` to each once you've reviewed its dry-run output.
`flickr-unbox <stage> --help` documents each command's full options.
New to this? `tools/build_private_test_fixture.py` (below) builds a small
fixture from your own real export to try this sequence on first.

## macOS: importing into Apple Photos (optional)

Everything above works on Windows/Mac/Linux and produces a flattened,
EXIF-restored directory that's useful with any photo app. If you're on a
Mac and specifically want that directory imported into **Apple Photos**,
five more commands are available — macOS-only (they drive Photos.app via
AppleScript, which doesn't exist elsewhere), and requiring one extra
dependency:

```
pip install -e ".[photos]"   # requires Python 3.10+ (osxphotos's own floor)
flickr-unbox doctor          # now also reports whether osxphotos is on PATH
```

Run in order, against the same `dest` the core pipeline finished with:

```
flickr-unbox photos-diff <dest> --out-dir <photos_diff_out/>
flickr-unbox photos-import <dest> --files-list <photos_diff_out/truly_missing.txt> --album "My Import"
flickr-unbox photos-verify --intended-files-list <photos_diff_out/truly_missing.txt> --log-file <photos_import_batch/photos_import.log>
flickr-unbox photos-retry <dest> --files-list <photos_verify_out/errors.txt> --album "My Import" --mode skip-dups
flickr-unbox photos-fix-dates --album "My Import" --source-dir <dest> --suspect-after <YYYY-MM-DD>
```

- **`photos-diff`** compares `dest` against your current Photos library by
  EXIF capture timestamp (not visual similarity — no third-party dedup
  tool needed) and writes `confirmed.txt`/`needs_review.txt`/
  `truly_missing.txt`/`no_exif_timestamp.txt`. `truly_missing.txt` is
  `photos-import`'s input.
- **`photos-import`** imports that list in chunks. **The screen must stay
  unlocked and the display must stay on for the entire run** — Photos'
  AppleScript automation stalls hard on display sleep/lock, confirmed by
  correlating real hangs against `pmset -g log`. A chunk exiting `rc=0`
  does **not** guarantee every file in it succeeded — always follow with:
- **`photos-verify`**, which reconciles the log against the intended list
  (diffs every logged outcome, not exit codes) and writes
  `errors.txt`/`missing.txt`/`orphans.txt`/etc. This is the only
  trustworthy completeness signal.
- **`photos-retry`** re-attempts stragglers from `photos-verify`'s output
  **one file at a time** — never batch former burst-siblings together, it
  re-triggers Photos' burst-photo grouping bug against each other (see
  below).
- **`photos-fix-dates`** corrects GIF/PNG (and other source-corrupted-tag)
  files that Photos silently mis-dated to the import date instead of
  their real capture date.

Two known, accepted, permanent gaps (not bugs in this tool — confirmed
Photos-app-level limitations):
- **Burst-group silent drop**: Photos collapses files sharing an embedded
  Apple burst-UUID tag down to one surviving asset during import, with
  zero log output for the rest. No osxphotos flag disables this.
- **GIF/PNG dates**: Photos doesn't read embedded EXIF dates for these
  formats on import at all (`photos-fix-dates` corrects it after the
  fact, but can't prevent it during import).

## Test fixtures

`tools/generate_test_fixtures.py` generates a small, fully synthetic test
corpus (`test_data/`) covering every edge case above, with a
hand-verified `expected_rename_plan.tsv` as ground truth. No real photos
or personal data — safe to commit and run in CI.

```
python3 tools/generate_test_fixtures.py
```

Requires `pip install pillow` for real JPEG bytes and `ffmpeg` on PATH
for a real `.mov` sample; falls back to placeholders for either if
missing (fine for filename-matching tests, not for exiftool write-back
tests).

See `test_data/MANIFEST.md` once generated for what each case tests.

## tools/ (dev tool, not part of the shipped pipeline)

`tools/build_private_test_fixture.py` builds a small, *private* local test
set from your own real Flickr export (one or more photo-batch zips +
the account's JSON info zips). Reach for it if you want to:

- **Try flickr-unbox on a small, fast subset before trusting it with your
  whole library.** The script prints the exact `flickr-unbox` command
  sequence to run next, so you can watch the real pipeline work end to
  end on data you own.
- **Validate a change you're making to this fork against real-world
  filenames/metadata**, not just the synthetic cases in `test_data/`.

It reuses the pipeline's own ID-matching logic
(`flickr_unbox.rename_plan.resolve_id`) rather than a separate
reimplementation, so the fixture it builds is guaranteed to reflect actual
pipeline behavior. Multiple photo/video zips (a real full export arrives
as several, e.g. `data-download-1.zip`, `data-download-2.zip`, ...) are
merged via the real `flatten` pipeline stage, so a multi-zip fixture
exercises collision-safe merging the same way a real migration would.

```
python tools/build_private_test_fixture.py <photo_source> [<photo_source> ...] <info_zips_dir> <work_dir>
```

Each `<photo_source>` is a photo/video export zip, or a directory
containing one or more of them (matched via `--photo-glob`, default
`data-download-*.zip`). `<photo_source>` and `<info_zips_dir>` can point
at the same directory — info zips are found by excluding anything that
matches `--photo-glob`, so no special folder layout is required. Run
`python tools/build_private_test_fixture.py --help` for the full options
and an example.

Output contains your real personal photo/EXIF data — it's local only,
never commit it (`.gitignore` already excludes `private_test_output/` and
`*_private/`; anything else you point `<work_dir>` at is on you to keep
private).

## License

MIT (see LICENSE). Not affiliated with Flickr or SmugMug.
