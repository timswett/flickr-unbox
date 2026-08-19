"""
Shared pre-flight validation result type.

Every stage runs the same checks in dry-run mode and in --no-dry-run mode --
--no-dry-run re-validates from current on-disk state rather than trusting an
earlier dry-run, since the data can change between the two invocations. This
type is what makes that guarantee real instead of aspirational: a stage
can't skip its own checks under --no-dry-run because there's only one check
path, not a duplicated one per mode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PreflightResult:
    ok: bool
    reasons: list = field(default_factory=list)

    @classmethod
    def passed(cls) -> "PreflightResult":
        return cls(ok=True)

    @classmethod
    def failed(cls, *reasons: str) -> "PreflightResult":
        return cls(ok=False, reasons=list(reasons))


def check_source_and_dest(source_base: Path, dest: Path) -> PreflightResult:
    """
    Shared "does the source dir exist, can we write to dest" check used by
    every stage that merges N source subfolders into one flat dest
    (`flatten`, `merge-photoinfo`). Factored out rather than duplicated so
    the two stay identical by construction.
    """
    reasons = []
    if not source_base.is_dir():
        reasons.append(f"source_base does not exist or is not a directory: {source_base}")

    if dest.exists():
        if not dest.is_dir():
            reasons.append(f"dest exists and is not a directory: {dest}")
        elif not os.access(dest, os.W_OK):
            reasons.append(f"dest is not writable: {dest}")
    elif not dest.parent.is_dir():
        reasons.append(f"dest does not exist and its parent doesn't either: {dest.parent}")
    elif not os.access(dest.parent, os.W_OK):
        reasons.append(f"dest's parent is not writable, can't create dest: {dest.parent}")

    return PreflightResult.failed(*reasons) if reasons else PreflightResult.passed()
