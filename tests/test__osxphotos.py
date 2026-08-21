"""Tests for flickr_unbox._osxphotos -- the shared platform/binary gate and
ANSI-stripping helper used by the optional photos-* stages."""
from flickr_unbox import _osxphotos


def test_is_macos_true_on_darwin():
    assert _osxphotos.is_macos(platform_fn=lambda: "darwin")


def test_is_macos_false_elsewhere():
    assert not _osxphotos.is_macos(platform_fn=lambda: "linux")
    assert not _osxphotos.is_macos(platform_fn=lambda: "win32")


def test_preflight_fails_on_non_macos_before_checking_binary():
    result = _osxphotos.preflight_platform_and_binary(
        "osxphotos",
        which_fn=lambda name: "/usr/local/bin/osxphotos",  # even if "found"
        platform_fn=lambda: "linux",
    )
    assert not result.ok
    assert "macOS-only" in result.reasons[0]


def test_preflight_fails_on_macos_with_missing_binary():
    result = _osxphotos.preflight_platform_and_binary(
        "osxphotos", which_fn=lambda name: None, platform_fn=lambda: "darwin"
    )
    assert not result.ok
    assert "not found on PATH" in result.reasons[0]
    assert "flickr-unbox[photos]" in result.reasons[0]


def test_preflight_passes_on_macos_with_binary_found():
    result = _osxphotos.preflight_platform_and_binary(
        "osxphotos", which_fn=lambda name: "/usr/local/bin/osxphotos", platform_fn=lambda: "darwin"
    )
    assert result.ok


def test_strip_ansi_removes_color_codes():
    raw = "\x1b[32mAdded\x1b[0m 12345.jpg to album"
    assert _osxphotos.strip_ansi(raw) == "Added 12345.jpg to album"


def test_strip_ansi_leaves_plain_text_untouched():
    assert _osxphotos.strip_ansi("Added 12345.jpg to album") == "Added 12345.jpg to album"


def test_strip_ansi_removes_non_color_csi_sequences_too():
    # Not just SGR/color ('m') sequences -- other CSI types (e.g. \x1b[2K
    # erase-line, common in leaked progress-bar output) must be stripped
    # too, or they'd silently defeat verify_photos_import.py's line regexes
    # the same way unstripped color codes once did in production.
    raw = "\x1b[2KAdded 12345.jpg to album\x1b[1A"
    assert _osxphotos.strip_ansi(raw) == "Added 12345.jpg to album"
