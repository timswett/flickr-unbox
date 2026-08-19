"""
gps-fix -- insert the missing decimal point into GPS coordinates in
renamed `<id>.json` sidecars.

Purpose:
    Flickr's export JSON stores latitude/longitude as an integer string
    with no decimal point (degrees * 1,000,000, e.g. `"42366702"` instead
    of `"42.366702"`). This stage rewrites those in place so exiftool
    (via `exif-write`) can parse them as real coordinates. Runs after
    `rename`, since it only looks at already-renamed `<id>.json` sidecars
    (matching `rename.sh`/`gps_fix.sh`'s real pipeline order).

Rewrite from bash (see FLICKR_UNBOX_HANDOFF.md TODO #1):
    The original `gps_fix.sh` used a grep guard (`"-?[0-9]+"`) plus a
    non-idempotent perl regex that always splits the *last 6 digits* off
    as the decimal part. That gets two cases wrong: a value with exactly
    6 digits loses its leading `0` (`"123456"` -> `.123456"` instead of
    `0.123456"`), and a value with fewer than 6 digits doesn't match
    `\\d{6}` at all and is silently left untouched -- no fix, no error, no
    log line. Neither has fired on Tim's real data, but they're real gaps
    in a regex built around "the last 6 digits" rather than the actual
    semantics (the raw integer is decimal_degrees * 1e6, for *any* digit
    count).

    This port replaces both the guard and the substitution with one
    parse-based, idempotent-by-construction operation: a value is only
    a fix target if it's still pure digits (optionally signed) -- a value
    that already contains a `.` structurally can't match, so there's no
    separate "already fixed?" check needed, unlike bash's two-step
    guard-then-substitute. The fix itself is pure string slicing (pad to
    >=7 digits, split 6 off the end as the decimal part), not
    `int(value) / 1e6` -- float division risks representation error
    (e.g. printing as `42.36670199999999`); string slicing is exact and
    handles any digit count uniformly, which is what actually closes both
    boundary cases above -- no length-based special-casing needed.

    Kept as a surgical text substitution (regex over the raw file text),
    not a `json.load`/`json.dump` round-trip -- reserializing the whole
    file risks changing formatting Flickr didn't (whitespace, key order,
    unicode escaping of things like accented characters in titles) even
    though only 1-2 values actually change. This also happens to fix a
    latent bug class the bash version had: `perl -pi` without `/g`
    processes line-by-line and only fixes the last match per line, so a
    hypothetical non-pretty-printed (compact, multi-field-per-line) JSON
    file would only get partially fixed. `re.sub` over the whole file
    text fixes every match regardless of how the file happens to be
    formatted.

    New: a `latitude`/`longitude` value that's neither already-decimal,
    empty, nor a clean optionally-signed digit string (garbage, scientific
    notation, etc.) is logged explicitly as an anomaly and left untouched,
    per this stage's pre-flight-table entry in the design doc -- bash's
    grep guard would just silently not match and do nothing, with no way
    to tell that happened short of noticing the file was never fixed.

Deliberately not ported: `gps_fix.sh` used `perl -pi.bak`, leaving a
    `.bak` backup next to every file it touched -- that's the exact source
    of the 20,140 stray `.bak` files flagged as cleanup debt in
    `CLAUDE_CODE_HANDOFF.md`. This port doesn't create them: dry-run
    already previews every change before `--no-dry-run` writes anything,
    which is the safety net used everywhere else in this design, and the
    fix is small/cheap to re-derive (only the GPS fields change; nothing
    is destroyed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from ._preflight import PreflightResult
from ._report import RunSummary

_SIDECAR_RE = re.compile(r"^[0-9]+\.json$")
_PRE_RENAME_SIDECAR_RE = re.compile(r"^photo_[0-9]+\.json$", re.IGNORECASE)
_FIELD_RE = re.compile(r'("(?:latitude|longitude)":\s*")([^"]*)(")')
_PURE_INT_RE = re.compile(r"^-?[0-9]+$")
_DECIMAL_RE = re.compile(r"^-?[0-9]+\.[0-9]+$")


def _insert_decimal(raw_digits: str) -> str:
    """'42366702' -> '42.366702'; '123456' -> '0.123456'; '1234' -> '0.001234'."""
    sign = ""
    if raw_digits.startswith("-"):
        sign, raw_digits = "-", raw_digits[1:]
    padded = raw_digits.zfill(7)  # guarantees >=1 integer digit + 6 decimal digits
    integer_part, decimal_part = padded[:-6], padded[-6:]
    return f"{sign}{integer_part}.{decimal_part}"


@dataclass
class FixResult:
    text: str
    fields_fixed: int = 0
    anomalies: List[str] = field(default_factory=list)  # raw anomalous values found


def fix_text(text: str) -> FixResult:
    """Apply the GPS decimal fix to raw JSON sidecar text. Pure function, no I/O."""
    result = FixResult(text=text)

    def repl(m: "re.Match") -> str:
        prefix, value, suffix = m.group(1), m.group(2), m.group(3)
        if _PURE_INT_RE.match(value):
            result.fields_fixed += 1
            return f"{prefix}{_insert_decimal(value)}{suffix}"
        if value == "" or _DECIMAL_RE.match(value):
            return m.group(0)  # no GPS data, or already fixed -- no-op either way
        result.anomalies.append(value)
        return m.group(0)  # leave untouched; logged by the caller

    result.text = _FIELD_RE.sub(repl, text)
    return result


def preflight(dest: Path) -> PreflightResult:
    if not dest.is_dir():
        return PreflightResult.failed(f"dest does not exist or is not a directory: {dest}")
    return PreflightResult.passed()


def run(dest: Path, dry_run: bool) -> RunSummary:
    summary = RunSummary(stage="gps-fix", dry_run=dry_run)

    result = preflight(dest)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    sidecars = sorted(p for p in dest.iterdir() if p.is_file() and _SIDECAR_RE.match(p.name))
    summary.bump("sidecars_scanned", len(sidecars))

    # This stage only recognizes already-renamed "<id>.json" sidecars (see
    # module docstring: it runs after `rename`). If pre-rename
    # "photo_<id>.json" sidecars are still present, sidecars_scanned would
    # otherwise silently read as 0 with no way to tell "already done" apart
    # from "run out of order" -- surface it explicitly instead.
    pre_rename = sorted(p for p in dest.iterdir() if p.is_file() and _PRE_RENAME_SIDECAR_RE.match(p.name))
    if pre_rename:
        summary.bump("pre_rename_sidecars_found", len(pre_rename))
        summary.note(
            f"WARNING: {len(pre_rename)} pre-rename 'photo_<id>.json' sidecar(s) found -- "
            "gps-fix only processes already-renamed '<id>.json' files. Run `rename` first, "
            "or check its COLLISION/UNRESOLVED rows if some files were left un-renamed."
        )

    for f in sidecars:
        fix = fix_text(f.read_text(encoding="utf-8"))
        for value in fix.anomalies:
            summary.note(f"ANOMALOUS VALUE (left untouched): {f.name}: {value!r}")
        summary.bump("anomalous_values", len(fix.anomalies))

        if fix.fields_fixed == 0:
            continue

        if dry_run:
            summary.bump("files_to_fix")
            summary.bump("fields_to_fix", fix.fields_fixed)
            continue

        try:
            f.write_text(fix.text, encoding="utf-8")
            summary.bump("files_fixed")
            summary.bump("fields_fixed", fix.fields_fixed)
        except OSError as e:
            summary.bump("failed")
            summary.note(f"FAILED: {f}: {e}")

    return summary
