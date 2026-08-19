"""
Tests for flickr_unbox.doctor.

Both the "found" and "missing" exiftool paths are exercised via injected
which_fn/version_fn, so this never depends on exiftool actually being
installed on the machine running the tests.
"""
from flickr_unbox import doctor


def test_check_reports_exiftool_found():
    result = doctor.check(
        exiftool_bin="exiftool",
        which_fn=lambda name: "/usr/local/bin/exiftool",
        version_fn=lambda name: "13.59",
    )
    assert result.exiftool_found
    assert result.exiftool_path == "/usr/local/bin/exiftool"
    assert result.exiftool_version == "13.59"
    assert result.python_version  # non-empty, real interpreter version


def test_check_reports_exiftool_missing():
    result = doctor.check(exiftool_bin="exiftool", which_fn=lambda name: None)
    assert not result.exiftool_found
    assert result.exiftool_path is None
    assert result.exiftool_version is None


def test_version_fn_not_called_when_exiftool_missing():
    calls = []
    doctor.check(
        exiftool_bin="exiftool",
        which_fn=lambda name: None,
        version_fn=lambda name: calls.append(name) or "13.59",
    )
    assert calls == []


def test_render_banner_when_found():
    result = doctor.check(
        which_fn=lambda name: "/usr/local/bin/exiftool", version_fn=lambda name: "13.59"
    )
    banner = doctor.render_banner(result)
    assert "13.59" in banner
    assert "/usr/local/bin/exiftool" in banner


def test_render_banner_when_missing():
    result = doctor.check(which_fn=lambda name: None)
    banner = doctor.render_banner(result)
    assert "not found" in banner
    assert "exiftool.org" in banner
