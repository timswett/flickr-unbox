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

The pipeline (8 stages, `src/flickr_unbox/`) is implemented in Python and
runnable via the `flickr-unbox` CLI, ported from a working bash/perl
pipeline that already processed a real ~100K-file / 300GB+ Flickr export
successfully. Every stage has been validated against real data, including
one full 500-file end-to-end run with zero errors — but the Python port
hasn't yet been run against a full real library at scale, and this repo
hasn't been published anywhere yet.

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
and a real 500-file sample end-to-end with zero errors (see "Status"
above). None of that amounts to a guarantee. It hasn't been run against
a full real library at scale, it hasn't been tested on every OS/
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

## Test fixtures

`generate_test_fixtures.py` generates a small, fully synthetic test
corpus (`test_data/`) covering every edge case above, with a
hand-verified `expected_rename_plan.tsv` as ground truth. No real photos
or personal data — safe to commit and run in CI.

```
python3 generate_test_fixtures.py
```

Requires `pip install pillow` for real JPEG bytes and `ffmpeg` on PATH
for a real `.mov` sample; falls back to placeholders for either if
missing (fine for filename-matching tests, not for exiftool write-back
tests).

See `test_data/MANIFEST.md` once generated for what each case tests.

## tools/ (dev tool, not part of the shipped pipeline)

`tools/build_private_test_fixture.py` builds a small, *private* local test
set from your own real Flickr export (one photo-batch zip + the account's
JSON info zips). Reach for it if you want to:

- **Try flickr-unbox on a small, fast subset before trusting it with your
  whole library.** The script prints the exact `flickr-unbox` command
  sequence to run next, so you can watch the real pipeline work end to
  end on data you own.
- **Validate a change you're making to this fork against real-world
  filenames/metadata**, not just the synthetic cases in `test_data/`.

It reuses the pipeline's own ID-matching logic
(`flickr_unbox.rename_plan.resolve_id`) rather than a separate
reimplementation, so the fixture it builds is guaranteed to reflect actual
pipeline behavior.

```
python tools/build_private_test_fixture.py <photo_zip> <info_zips_dir> <work_dir>
```

Output contains your real personal photo/EXIF data — it's local only,
never commit it (`.gitignore` already excludes `private_test_output/` and
`*_private/`; anything else you point `<work_dir>` at is on you to keep
private).

## License

MIT (see LICENSE). Not affiliated with Flickr or SmugMug.
