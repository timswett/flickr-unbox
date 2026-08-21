# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

flickr-unbox restores real EXIF/IPTC metadata (title, description, date,
GPS, tags) into a Flickr data export, matching each photo/video to its
separately-exported `photo_<id>.json` sidecar and writing the data in with
`exiftool`, at up to ~100K-file / 300GB+ scale without running the disk out
of space. See `README.md` for the full problem description and the
non-obvious edge cases (ambiguous filename→ID matching, non-decimal GPS
coordinates, exiftool's `_original` backup doubling disk usage) that drive
the design below.

## Commands

```
pip install -e ".[dev]"          # editable install + pytest
python -m pytest tests/          # full suite (hermetic, no real data or exiftool needed)
python -m pytest tests/test_gps_fix.py::test_name   # single test
flickr-unbox doctor              # check exiftool is on PATH
flickr-unbox <stage> --help      # per-subcommand options
```

Two dev-only extras beyond `pytest`: `pip install pillow` (for
`tools/generate_test_fixtures.py` to emit real JPEG bytes) and `exiftool`
on PATH (for `exif-write` and anything downstream of it — `doctor` checks
this). A third, optional and macOS-only: `pip install -e ".[photos]"`
(needs Python 3.10+) pulls in `osxphotos` for the `photos-*` subcommands
that import `dest` into Apple Photos — not needed for the core pipeline
or its test suite, which stay dependency-free either way.

`bandit -r src/ -c pyproject.toml` is the security-scan command; expected
to report 0 issues (findings are fixed or suppressed inline with
`# nosec <code>` + a reason, never silently skipped — see `[tool.bandit]`
in `pyproject.toml`).

## Architecture

**One CLI, one subcommand per pipeline stage**, run in this fixed order —
cross-platform (Windows/Mac/Linux), stdlib-only:

```
flatten → merge-photoinfo → rename-plan → rename → gps-fix → exif-batches → exif-write → cleanup
```

Beyond that, five more subcommands exist for the optional, macOS-only
step of importing `dest` into Apple Photos — see "Optional, macOS-only
tail" below before touching any `photos_*.py` module.

Each stage is `src/flickr_unbox/<stage>.py` with a `run(...) -> RunSummary`
entry point that `cli.py` wires to an argparse subcommand — `cli.py` is
deliberately thin (see its own module docstring) and contains no pipeline
logic itself, just argument parsing and dispatch. Two subcommands
(`flatten`, `merge-photoinfo`) share one argparse-wiring helper
(`_add_merge_style_subcommand`) because they're the same "merge N source
subfolders into one flat dest" shape with only a file filter differing;
`rename-plan`/`rename` share another (`_add_dest_plan_subcommand`) for the
"write a plan, then execute it" shape.

**Safety model — read this before touching any stage's control flow.**
Every subcommand defaults to a dry run: full pre-flight validation +
full summary printed, nothing on disk changes. `--no-dry-run` is required
to act for real, and critically **re-runs the same validation from
current on-disk state** rather than trusting an earlier dry-run — data can
change between the two invocations. This is why pre-flight logic lives in
one shared path per stage, not duplicated per mode: a stage physically
can't skip its own checks under `--no-dry-run`.

**Optional, macOS-only tail beyond the core 8 stages**: importing `dest`
into Apple Photos. These five subcommands (`photos-diff -> photos-import
-> photos-verify -> [photos-retry] -> [photos-fix-dates]`) live in their
own modules (`photos_diff.py`, `photos_import.py`,
`verify_photos_import.py`, `fix_media_dates.py`, sharing
`_osxphotos.py`) and are never imported by the core pipeline modules or
vice versa. They exist because Apple Photos/AppleScript automation is
macOS-only, needs the extra `osxphotos` dependency (`pip install
".[photos]"`, Python 3.10+), and behaves differently from the rest of
this codebase in one important way: **`rc=0`/a clean exit from
`photos-import` or `photos-retry` does not mean every file succeeded** --
Photos' own AppleScript automation can silently lose files around an
internal auto-recovery, and separately silently drops non-representative
files from "burst groups" with zero log output at all. `photos-verify`,
which diffs the intended file list against every logged outcome in the
run's log file, is the only trustworthy completeness signal for that
pair of commands -- always run it after, don't trust a green exit code
the way you more safely can for `exif-write`/`cleanup`. See
`photos_import.py`'s module docstring for the full list of hard-won
lessons (screen-lock/display-sleep hangs, the bash-3.2 `mapfile` gotcha
this was originally ported around, etc.) and README.md's "macOS:
importing into Apple Photos" section for the user-facing version.

**Shared internal plumbing** (`src/flickr_unbox/_*.py`, imported by
multiple stage modules, no CLI subcommand of their own):
- `_report.py` — `RunSummary`: the counts/notes block every stage prints
  at the end of a run, in both dry-run and real mode.
- `_preflight.py` — `PreflightResult` plus `check_source_and_dest()`, the
  shared source-exists/dest-writable check used by `flatten` and
  `merge-photoinfo`.
- `_collision_merge.py` — the actual collision-safe move-planning/
  execution logic (`plan_moves`, `execute_moves`,
  `remove_empty_source_dirs`, `find_junk`) that both `flatten.py` and
  `merge_photoinfo.py` call into, factored out once it became clear they
  were the same algorithm with a different file filter. Fix a bug here
  once, both stages get the fix — don't reimplement per stage.
- `_osxphotos.py` — the macOS-only tail's equivalent shared plumbing:
  `preflight_platform_and_binary()` (the platform + `osxphotos`-on-PATH
  gate every `photos_*.py` stage's own `preflight()` calls into) and
  `strip_ansi()` (osxphotos's `-V` output embeds raw ANSI escapes even
  when redirected to a file — `verify_photos_import.py` strips them
  before parsing; forgetting this once caused a real mis-analysis in the
  original migration, see that module's docstring).

**`cleanup`'s receipt gate is the one deliberate behavior change from the
original bash pipeline**, not just a mechanical port: it refuses to delete
a batch's `exiftool _original` backups unless `exif_write.py`'s own
`batch_dir/batch_{NN}.result.json` receipt (written after every real
`exif-write` run) confirms zero errors for that exact batch — with
distinct refusal reasons for a missing receipt, a receipt showing errors,
and a stale receipt (batch file changed since `exif-write` last ran).

**`rename_plan.resolve_id()` / `photo_json_id()`** are the pipeline's
canonical filename→ID matching logic and are public specifically so
`tools/build_private_test_fixture.py` can import and reuse them rather
than hand-maintaining a second copy that could drift out of sync — if you
touch matching behavior, that tool's fixtures reflect the change
automatically.

## Two-tier testing

1. **`tests/`** — synthetic, hermetic, no real data or `exiftool` needed;
   this is what CI runs (and what you should run after any change).
   Fixtures live in `test_data/`, generated by
   `tools/generate_test_fixtures.py` (regenerate with `rm -rf test_data &&
   python3 tools/generate_test_fixtures.py` from the repo root — its
   output is committed, so confirm `git diff` is empty after
   regenerating unless you meant to change a fixture).
2. **Real-data validation** — ad hoc, not part of the committed suite
   (needs a real Flickr export, can't run in CI). `tools/
   build_private_test_fixture.py` builds a small private fixture from a
   real export; run the full CLI chain against it end to end before
   trusting a pipeline-logic change at scale.

Never commit anything under `private_test_output/`, `*_private/`, or any
other real-export-derived output — real personal EXIF/GPS/photo data, kept
out of the repo by `.gitignore` by design.

## Reference docs in this repo

- `README.md` — user-facing: why this exists, installation, usage,
  disclaimer.
- `CONTRIBUTING.md` — project conventions (commenting, commit style,
  folder structure).
