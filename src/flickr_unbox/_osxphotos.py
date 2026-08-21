"""
Shared plumbing for the optional, macOS-only "photos-*" stages
(photos_diff, photos_import, fix_media_dates -- verify_photos_import is
pure text processing and doesn't need this module at all).

These stages talk to Apple Photos via the `osxphotos` CLI, which drives
Photos.app over AppleScript -- something that only exists on macOS. Every
stage that shells out to `osxphotos` gates on both "are we on macOS" and
"is the binary on PATH" before doing anything else, with a distinct
message for each (a Windows/Linux user needs "this feature doesn't exist
on your OS", not the same "binary not found, install it" message a Mac
user with a missing install would get).
"""
from __future__ import annotations

import re
import shutil
import sys
from typing import Callable, Optional

from ._preflight import PreflightResult

WhichFn = Callable[[str], Optional[str]]
PlatformFn = Callable[[], str]

# Any ANSI CSI sequence (ESC [ ... <final byte>), not just SGR/color codes
# (which end in 'm' specifically) -- redirected -V output could plausibly
# also leak other CSI types (e.g. \x1b[2K erase-line, common in progress-bar
# output), and those would silently defeat downstream parsing the same way
# color codes did if this only matched 'm'.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def is_macos(platform_fn: PlatformFn = lambda: sys.platform) -> bool:
    return platform_fn() == "darwin"


def preflight_platform_and_binary(
    osxphotos_bin: str,
    which_fn: WhichFn = shutil.which,
    platform_fn: PlatformFn = lambda: sys.platform,
) -> PreflightResult:
    if not is_macos(platform_fn):
        return PreflightResult.failed(
            "Apple Photos import is macOS-only -- Photos.app and the AppleScript "
            "automation osxphotos drives don't exist on this platform"
        )
    if which_fn(osxphotos_bin) is None:
        return PreflightResult.failed(
            f"osxphotos binary not found on PATH: {osxphotos_bin!r} -- "
            'install it with `pip install "flickr-unbox[photos]"` (requires Python 3.10+)'
        )
    return PreflightResult.passed()


def strip_ansi(text: str) -> str:
    """
    Strip ANSI color escape codes. osxphotos's -V output embeds raw escape
    bytes directly around filenames/UUIDs even when stdout is redirected to
    a file, not just on a TTY -- a naive grep/regex against the raw text
    silently breaks (confirmed the hard way: a real run once reported
    "0 files added" against a log that actually had 7,228 successes,
    because the escape bytes broke the match). Always strip before parsing
    osxphotos log output.
    """
    return _ANSI_ESCAPE_RE.sub("", text)
