"""
rename-plan -- compute the old-name -> <id>.<ext> rename mapping for a
flattened Flickr export directory. Read-only: never touches the real
photo/video/JSON files, only writes a plan file for `rename` to execute.

Purpose:
    Flickr filenames come in several shapes that don't consistently carry
    the photo ID in an unambiguous position:
      - `<id>_<secret>_o.ext`
      - `video_<id>.ext`
      - `<slug>_<id>_o.ext` (no secret)
      - `<basefilename>_<id>_o.ext` (no secret)
    A 10-digit "secret" can look identical to a photo ID by shape alone, so
    position/digit-length matching alone is ambiguous. This stage instead
    generates ID *candidates* from each filename and verifies each one
    against the real `photo_<id>.json` sidecar IDs actually present in the
    directory -- a candidate that doesn't correspond to a real sidecar is
    rejected, resolving the ambiguity in practice (100% match rate on a
    real ~50K-file export).

Edge cases this exists to handle (see FLICKR_UNBOX_HANDOFF.md TODOs #2, #3):
    - Collision: two different old names would both compute to the same
      new `<id>.<ext>`. Detected and marked COLLISION, not silently
      resolved -- both sides are left alone by `rename` rather than one
      overwriting the other. (0 real collisions ever observed, but the
      check exists so it fails loud instead of silently if one ever does.)
    - Ambiguous double-hit: in `<seg1>_<seg2>_o.ext`, if *both* segments
      are purely numeric and *both* happen to match a real sidecar ID
      (rare, but possible -- e.g. one photo's ID appears as another
      photo's "secret"-shaped segment), candidates are checked in the
      order [last segment, second-to-last segment] and the first match
      wins -- i.e. the **last segment silently wins** the tie. This exact
      order must be preserved; it's easy to "obviously" reimplement this
      as first-match instead of last-match. Covered by
      test_rename_plan.py's parity test against the golden-master fixture.

Row order note:
    The original `build_rename_plan.pl` writes UNRESOLVED rows to the plan
    file immediately during its first pass (before it knows which OK rows
    are actually COLLISIONs), then appends all OK/COLLISION rows in a
    second pass -- so its raw output file has all UNRESOLVED rows first,
    then all OK/COLLISION rows, both in directory-sorted order within
    their own block. That split was an artifact of needing two passes to
    know collision counts, not a deliberate design choice, and no fixture
    or downstream script depends on it (`rename.py` reads the whole plan
    regardless of order). This port uses a single sorted-by-old-name order
    instead, which is simpler and easier to scan by eye.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ._collision_merge import is_junk
from ._preflight import PreflightResult
from ._report import RunSummary

_PHOTO_JSON_RE = re.compile(r"^photo_(\d+)\.json$", re.IGNORECASE)
_VIDEO_RE = re.compile(r"^video_(\d+)\.\w+$", re.IGNORECASE)
_O_SUFFIX_RE = re.compile(r"_o\.\w+$", re.IGNORECASE)
_EXT_RE = re.compile(r"\.(\w+)$")
_ID_LIKE_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class PlanRow:
    status: str  # "OK" | "COLLISION" | "UNRESOLVED"
    old: str
    new: str  # "" for UNRESOLVED


def photo_json_id(filename: str) -> Optional[str]:
    """The <id> if filename is a photo_<id>.json sidecar (case-insensitive), else None."""
    m = _PHOTO_JSON_RE.match(filename)
    return m.group(1) if m else None


def resolve_id(filename: str, json_ids: set) -> Optional[str]:
    """
    The real pipeline's ID-matching rule, public because it's the one piece
    of logic `tools/build_private_test_fixture.py` needs to reuse rather
    than reimplement -- see that script's docstring for why.
    """
    m = _VIDEO_RE.match(filename)
    if m:
        return m.group(1)

    if not _O_SUFFIX_RE.search(filename):
        return None

    remainder = _O_SUFFIX_RE.sub("", filename)
    parts = remainder.split("_")

    candidates = []
    if len(parts) >= 1 and _ID_LIKE_RE.match(parts[-1]):
        candidates.append(parts[-1])
    if len(parts) >= 2 and _ID_LIKE_RE.match(parts[-2]):
        candidates.append(parts[-2])

    for candidate in candidates:  # last segment checked first -- last wins on a tie
        if candidate in json_ids:
            return candidate
    return None


def build_plan(dest: Path) -> List[PlanRow]:
    """Compute the full rename plan for dest without touching any files."""
    # Unlike flatten/merge-photoinfo/exif-batches/exif-write, this stage
    # previously didn't filter junk -- an AppleDouble file like
    # "._<id>_<secret>_o.jpg" would resolve to the same numeric ID as its
    # real counterpart, marking BOTH rows COLLISION and leaving the real
    # photo un-renamed.
    files = sorted(p.name for p in dest.iterdir() if p.is_file() and not is_junk(p))

    json_ids = set()
    for f in files:
        json_id = photo_json_id(f)
        if json_id:
            json_ids.add(json_id)

    new_name_count: Dict[str, int] = {}
    # Each entry is either a finished PlanRow (UNRESOLVED) or a
    # (old, new) pair still pending OK/COLLISION classification, which
    # requires having seen every row's new-name first.
    pending_slots: List[object] = []

    for f in files:
        json_id = photo_json_id(f)
        if json_id:
            new = f"{json_id}.json"
            pending_slots.append((f, new))
            new_name_count[new] = new_name_count.get(new, 0) + 1
            continue

        if f.lower().endswith(".json"):
            continue  # account-level export (albums.json, contacts_*.json, ...) -- not a sidecar

        id_ = resolve_id(f, json_ids)
        if id_ is None:
            pending_slots.append(PlanRow("UNRESOLVED", f, ""))
            continue

        ext_match = _EXT_RE.search(f)
        ext = ext_match.group(1).lower()
        new = f"{id_}.{ext}"
        pending_slots.append((f, new))
        new_name_count[new] = new_name_count.get(new, 0) + 1

    rows: List[PlanRow] = []
    for slot in pending_slots:
        if isinstance(slot, PlanRow):
            rows.append(slot)
        else:
            old, new = slot
            status = "COLLISION" if new_name_count[new] > 1 else "OK"
            rows.append(PlanRow(status, old, new))

    return rows


def write_plan(rows: List[PlanRow], plan_path: Path) -> None:
    with open(plan_path, "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["status", "old_name", "new_name"])
        for row in rows:
            writer.writerow([row.status, row.old, row.new])


def preflight(dest: Path, plan_path: Path) -> PreflightResult:
    reasons = []
    if not dest.is_dir():
        reasons.append(f"dest does not exist or is not a directory: {dest}")
    if not os.access(plan_path.parent, os.W_OK):
        reasons.append(f"plan_path's parent is not writable: {plan_path.parent}")
    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()


def run(dest: Path, plan_path: Path, dry_run: bool) -> RunSummary:
    summary = RunSummary(stage="rename-plan", dry_run=dry_run)

    result = preflight(dest, plan_path)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    rows = build_plan(dest)
    summary.bump("ok", sum(1 for r in rows if r.status == "OK"))
    summary.bump("collision", sum(1 for r in rows if r.status == "COLLISION"))
    summary.bump("unresolved", sum(1 for r in rows if r.status == "UNRESOLVED"))
    for r in rows:
        if r.status == "COLLISION":
            summary.note(f"COLLISION: {r.old} -> {r.new}")
        elif r.status == "UNRESOLVED":
            summary.note(f"UNRESOLVED: {r.old}")

    if dry_run:
        summary.note(f"plan not written (dry run) -- would write {plan_path}")
        return summary

    write_plan(rows, plan_path)
    summary.note(f"plan written to {plan_path}")
    return summary
