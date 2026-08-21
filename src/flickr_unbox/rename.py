"""
rename -- execute a rename plan computed by rename-plan.

Purpose:
    Applies the `<old_name> -> <id>.<ext>` mapping from a `rename-plan` TSV
    to the real files in `dest`. Only `OK` rows are ever acted on;
    `COLLISION` and `UNRESOLVED` rows are left in place for a human to
    resolve.

Safety model -- stale-plan detection (new behavior, not in `rename.sh`):
    A plan file is a snapshot of `dest` at the moment `rename-plan` ran.
    If anything in `dest` changes afterward -- a file added, removed, or
    renamed by something else -- the plan no longer describes reality, and
    blindly executing it could rename the wrong things or leave the
    directory in a mixed state. `rename.sh` has no defense against this;
    it just runs whatever the plan says. This port's pre-flight instead
    **recomputes** what `rename-plan` would produce for `dest`'s current
    contents and compares it against the loaded plan file, row for row. Any
    difference means the plan is stale, and the whole run refuses to start
    (not just skip the affected rows) until `rename-plan` is re-run.

    The per-row MISSING SOURCE / target-collision checks below (ported
    from `rename.sh`) still exist on top of that as defense-in-depth
    against a race between the pre-flight snapshot and the actual move
    loop -- narrow window, but cheap to guard and matches the
    log-and-continue convention (CONTRIBUTING.md) rather than aborting the
    whole run over one file.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List

from . import rename_plan as rename_plan_module
from ._preflight import PreflightResult
from ._report import RunSummary

PlanRow = rename_plan_module.PlanRow


def load_plan(plan_path: Path) -> List[PlanRow]:
    with open(plan_path, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return [PlanRow(row["status"], row["old_name"], row["new_name"]) for row in reader]


def preflight(dest: Path, plan_path: Path) -> PreflightResult:
    if not plan_path.is_file():
        return PreflightResult.failed(f"plan file not found: {plan_path}")
    if not dest.is_dir():
        return PreflightResult.failed(f"dest does not exist or is not a directory: {dest}")

    plan_rows = {(r.status, r.old, r.new) for r in load_plan(plan_path)}
    current_rows = {(r.status, r.old, r.new) for r in rename_plan_module.build_plan(dest)}

    if plan_rows != current_rows:
        stale_in_plan = len(plan_rows - current_rows)
        new_since_plan = len(current_rows - plan_rows)
        return PreflightResult.failed(
            "plan is stale -- dest's contents have changed since rename-plan was run "
            f"({stale_in_plan} row(s) in the plan no longer match current dest, "
            f"{new_since_plan} row(s) reflect a state the plan doesn't know about). "
            "Re-run rename-plan and review the new plan before renaming."
        )

    return PreflightResult.passed()


def execute_plan(dest: Path, rows: List[PlanRow], dry_run: bool, summary: RunSummary) -> None:
    for row in rows:
        if row.status != "OK":
            summary.bump(f"skipped_{row.status.lower()}")
            continue

        src = dest / row.old
        target = dest / row.new

        if not src.exists():
            summary.bump("missing_source")
            summary.note(f"MISSING SOURCE: {src}")
            continue
        if target.exists() and src != target:
            summary.bump("collision_at_move_time")
            summary.note(f"COLLISION (target already exists, skipping): {src} -> {target}")
            continue

        if dry_run:
            summary.bump("would_rename")
            continue

        try:
            src.rename(target)
            summary.bump("renamed")
        except OSError as e:
            summary.bump("failed")
            summary.note(f"FAILED: {src} -> {target}: {e}")


def run(dest: Path, plan_path: Path, dry_run: bool) -> RunSummary:
    summary = RunSummary(stage="rename", dry_run=dry_run)

    result = preflight(dest, plan_path)
    if not result.ok:
        for reason in result.reasons:
            summary.note(f"PREFLIGHT FAILED: {reason}")
        summary.bump("preflight_failed")
        return summary

    rows = load_plan(plan_path)
    execute_plan(dest, rows, dry_run, summary)
    return summary
