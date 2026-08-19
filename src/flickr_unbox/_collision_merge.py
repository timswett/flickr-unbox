"""
Shared "merge N source subfolders into one flat dest, collision-safe" logic.

Both `flatten` (all non-junk files, from `data-download-*` zip folders) and
`merge-photoinfo` (JSON sidecars only, from `*part*` PhotoInformation
folders) are the same algorithm with a different file filter: walk each
matched source subfolder, and on a filename collision with something
already claimed, fall back to a `<source-folder-name>_<filename>` prefix
rather than overwriting. Factored out here instead of duplicated per module
-- two independent copies of the claimed/reserved collision bookkeeping is
exactly the kind of drift that's easy to get subtly wrong in only one of
them.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, List, Set, Tuple

from ._report import RunSummary

_JUNK_NAMES = {".DS_Store"}


def is_junk(path: Path) -> bool:
    return path.name.startswith("._") or path.name in _JUNK_NAMES


def find_junk(source_base: Path) -> List[Path]:
    """All AppleDouble/.DS_Store junk files anywhere under source_base."""
    return sorted(p for p in source_base.rglob("*") if p.is_file() and is_junk(p))


def plan_moves(
    source_base: Path,
    dest: Path,
    source_glob: str,
    file_predicate: Callable[[Path], bool],
) -> Tuple[List[Tuple[Path, Path, bool]], List[Tuple[Path, str]]]:
    """
    Build the full source -> target move plan without touching disk.

    Returns (moves, conflicts):
      moves: list of (src, target, was_collision_renamed)
      conflicts: list of (src, reason) -- files that can't be placed even
        after the collision-prefix fallback; excluded from moves and not
        fatal to the run (see CONTRIBUTING.md "log-and-continue").

    An in-memory `claimed` set (existing dest files) plus a `reserved` set
    (targets already spoken for by an earlier row in this same plan) stands
    in for the growing dest directory the bash originals observed live,
    move-by-move -- necessary here so dry-run can compute an accurate plan
    without any real I/O.
    """
    moves: List[Tuple[Path, Path, bool]] = []
    conflicts: List[Tuple[Path, str]] = []

    claimed: Set[str] = {p.name for p in dest.glob("*")} if dest.is_dir() else set()
    reserved: Set[str] = set()

    for parent_dir in sorted(source_base.glob(source_glob)):
        if not parent_dir.is_dir():
            continue
        for src in sorted(p for p in parent_dir.rglob("*") if p.is_file()):
            if is_junk(src) or not file_predicate(src):
                continue
            name = src.name
            renamed = False
            if name in claimed or name in reserved:
                name = f"{parent_dir.name}_{src.name}"
                renamed = True
            if name in claimed or name in reserved:
                conflicts.append(
                    (src, f"target already exists even after collision-prefix: {name}")
                )
                continue
            reserved.add(name)
            moves.append((src, dest / name, renamed))

    return moves, conflicts


def execute_moves(moves: List[Tuple[Path, Path, bool]], summary: RunSummary) -> None:
    """Perform a planned move list for real, log-and-continue on failure."""
    moved = 0
    failed = 0
    for src, target, _renamed in moves:
        try:
            shutil.move(str(src), str(target))
            moved += 1
        except OSError as e:
            failed += 1
            summary.note(f"FAILED: {src} -> {target}: {e}")
    summary.bump("moved", moved)
    summary.bump("failed", failed)


def remove_empty_source_dirs(source_base: Path, source_glob: str) -> int:
    """Remove now-empty subfolders (and their now-empty parents) after a real merge."""
    removed = 0
    for parent_dir in sorted(source_base.glob(source_glob), reverse=True):
        if not parent_dir.is_dir():
            continue
        for d in sorted(parent_dir.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                removed += 1
        if parent_dir.is_dir() and not any(parent_dir.iterdir()):
            parent_dir.rmdir()
            removed += 1
    return removed
