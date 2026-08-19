"""
doctor -- check that the local environment has what flickr-unbox needs.

The pipeline itself has zero third-party pip dependencies (stdlib only --
see pyproject.toml, `pytest`/`pillow` are dev/testing-only extras, not
needed to run the CLI). Python 3.9+ is already enforced by `pip install`
refusing to run on an older interpreter, so the one real external
prerequisite worth checking at runtime is the `exiftool` binary, needed by
`exif-write`.

Two ways this runs:
  1. `flickr-unbox doctor` -- an explicit, standalone check a new user (or
     anyone troubleshooting) can run first, with the full RunSummary-style
     output every other subcommand uses.
  2. A short one-line banner automatically printed before *every* other
     subcommand, in both dry-run and --no-dry-run mode -- so a multi-step
     workflow surfaces a missing `exiftool` immediately, rather than 5
     stages in when `exif-write` finally needs it. This banner is
     informational only: it never blocks a stage that doesn't actually
     need exiftool (most of them don't) from running. `exif-write`'s own
     pre-flight is still the real, blocking gate for that specific stage.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from ._report import RunSummary

WhichFn = Callable[[str], Optional[str]]
VersionFn = Callable[[str], Optional[str]]


@dataclass
class DoctorResult:
    python_version: str
    exiftool_bin: str
    exiftool_path: Optional[str]
    exiftool_version: Optional[str]

    @property
    def exiftool_found(self) -> bool:
        return self.exiftool_path is not None


def _default_exiftool_version(exiftool_bin: str) -> Optional[str]:
    try:
        # List-form argv, no shell=True; exiftool_bin is a fixed default or an
        # explicit CLI flag, never derived from file/data content.
        result = subprocess.run(  # nosec B603
            [exiftool_bin, "-ver"], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def check(
    exiftool_bin: str = "exiftool",
    which_fn: WhichFn = shutil.which,
    version_fn: VersionFn = _default_exiftool_version,
) -> DoctorResult:
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    exiftool_path = which_fn(exiftool_bin)
    exiftool_version = version_fn(exiftool_bin) if exiftool_path else None
    return DoctorResult(
        python_version=python_version,
        exiftool_bin=exiftool_bin,
        exiftool_path=exiftool_path,
        exiftool_version=exiftool_version,
    )


def render_banner(result: DoctorResult) -> str:
    """One line, printed automatically before every other subcommand."""
    if result.exiftool_found:
        return f"[doctor] exiftool {result.exiftool_version} found at {result.exiftool_path}"
    return (
        f"[doctor] exiftool not found on PATH ({result.exiftool_bin!r}) -- "
        "only needed for the exif-write stage; install from https://exiftool.org/"
    )


def run(exiftool_bin: str = "exiftool") -> RunSummary:
    """Full check, used by the standalone `flickr-unbox doctor` subcommand."""
    result = check(exiftool_bin)
    summary = RunSummary(stage="doctor", dry_run=False)
    summary.note(f"python: {result.python_version}")
    if result.exiftool_found:
        summary.note(f"exiftool: {result.exiftool_version} at {result.exiftool_path}")
        summary.bump("ok")
    else:
        summary.note(
            f"exiftool: NOT FOUND on PATH ({exiftool_bin!r}) -- "
            "required for the exif-write stage; install from https://exiftool.org/"
        )
        summary.bump("missing")
    return summary
