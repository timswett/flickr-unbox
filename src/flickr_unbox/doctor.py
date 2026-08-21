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
     output every other subcommand uses. This is the only place the
     optional `osxphotos` check (below) is surfaced.
  2. A short one-line banner automatically printed before *every* other
     subcommand, in both dry-run and --no-dry-run mode -- so a multi-step
     workflow surfaces a missing `exiftool` immediately, rather than 5
     stages in when `exif-write` finally needs it. This banner is
     informational only: it never blocks a stage that doesn't actually
     need exiftool (most of them don't) from running. `exif-write`'s own
     pre-flight is still the real, blocking gate for that specific stage.
     This banner stays exiftool-only -- it does not also check osxphotos.

Why osxphotos isn't in the universal banner: it's only needed by the
optional, macOS-only "photos-*" stages (see photos_diff.py/
photos_import.py/fix_media_dates.py), and most invocations of this CLI
are neither on macOS nor using those stages -- printing "osxphotos not
found" before every `flatten`/`rename`/etc. call on a Windows/Linux
machine would be noise, not signal. `flickr-unbox doctor` reports it
explicitly instead, and only as a real finding when running on macOS;
elsewhere it's reported as not applicable, not as a problem. Each
photos-* stage's own preflight() is the actual blocking gate, same
principle as exiftool/exif-write.
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
PlatformFn = Callable[[], str]


@dataclass
class DoctorResult:
    python_version: str
    exiftool_bin: str
    exiftool_path: Optional[str]
    exiftool_version: Optional[str]
    # Defaulted (unlike the exiftool fields above): existing callers that
    # construct a DoctorResult directly for exiftool-only assertions
    # (see tests/test_cli.py) shouldn't have to know about the osxphotos
    # fields added alongside the optional photos-* stages.
    is_macos: bool = False
    osxphotos_bin: str = "osxphotos"
    osxphotos_path: Optional[str] = None
    osxphotos_version: Optional[str] = None

    @property
    def exiftool_found(self) -> bool:
        return self.exiftool_path is not None

    @property
    def osxphotos_found(self) -> bool:
        return self.osxphotos_path is not None


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


def _default_osxphotos_version(osxphotos_bin: str) -> Optional[str]:
    try:
        # List-form argv, no shell=True; same reasoning as exiftool above.
        result = subprocess.run(  # nosec B603
            [osxphotos_bin, "--version"], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def check(
    exiftool_bin: str = "exiftool",
    which_fn: WhichFn = shutil.which,
    version_fn: VersionFn = _default_exiftool_version,
    osxphotos_bin: str = "osxphotos",
    osxphotos_version_fn: VersionFn = _default_osxphotos_version,
    platform_fn: PlatformFn = lambda: sys.platform,
) -> DoctorResult:
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    exiftool_path = which_fn(exiftool_bin)
    exiftool_version = version_fn(exiftool_bin) if exiftool_path else None

    is_macos = platform_fn() == "darwin"
    # Only actually probed on macOS -- on other platforms it's not
    # applicable at all, not a "not found" finding (see module docstring).
    osxphotos_path = which_fn(osxphotos_bin) if is_macos else None
    osxphotos_version = osxphotos_version_fn(osxphotos_bin) if osxphotos_path else None

    return DoctorResult(
        python_version=python_version,
        exiftool_bin=exiftool_bin,
        exiftool_path=exiftool_path,
        exiftool_version=exiftool_version,
        is_macos=is_macos,
        osxphotos_bin=osxphotos_bin,
        osxphotos_path=osxphotos_path,
        osxphotos_version=osxphotos_version,
    )


def render_banner(result: DoctorResult) -> str:
    """One line, printed automatically before every other subcommand."""
    if result.exiftool_found:
        return f"[doctor] exiftool {result.exiftool_version} found at {result.exiftool_path}"
    return (
        f"[doctor] exiftool not found on PATH ({result.exiftool_bin!r}) -- "
        "only needed for the exif-write stage; install from https://exiftool.org/"
    )


def run(exiftool_bin: str = "exiftool", osxphotos_bin: str = "osxphotos") -> RunSummary:
    """Full check, used by the standalone `flickr-unbox doctor` subcommand."""
    result = check(exiftool_bin, osxphotos_bin=osxphotos_bin)
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

    if not result.is_macos:
        summary.note("osxphotos: N/A -- the optional photos-* stages are macOS-only")
    elif result.osxphotos_found:
        summary.note(f"osxphotos: {result.osxphotos_version} at {result.osxphotos_path}")
        summary.bump("ok")
    else:
        summary.note(
            f"osxphotos: NOT FOUND on PATH ({result.osxphotos_bin!r}) -- only needed for the "
            'optional photos-diff/photos-import/photos-retry/photos-fix-dates stages; install '
            'with `pip install "flickr-unbox[photos]"` (requires Python 3.10+)'
        )
        # Deliberately a different counter than exiftool's "missing" above:
        # osxphotos is optional (only the photos-* stages need it), so a
        # missing install here shouldn't flip doctor's own exit code the
        # way a missing exiftool -- a hard prerequisite for exif-write --
        # does. See _run_doctor in cli.py, which only checks "missing".
        summary.bump("optional_missing")

    return summary
