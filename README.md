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

Early — scripts are being generalized from a working pipeline that's
already processed a real ~100K-file / 300GB+ Flickr export successfully.
Not yet packaged for drop-in use; paths/config are still being
parameterized.

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

## tools/ (maintainer-only, not part of the published pipeline)

`tools/build_private_test_fixture.sh` + `tools/match_photos_to_json.pl`
let a maintainer build a *private* local test set from their own real
Flickr export (one photo-batch zip + the account's JSON info zips), for
validating changes against real-world data before a release. Output
contains real personal photo/EXIF data — never commit it.

## License

MIT (see LICENSE). Not affiliated with Flickr or SmugMug.
