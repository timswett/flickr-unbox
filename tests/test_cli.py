"""
Tests for flickr_unbox.cli's shared main() banner logic.

Only the exiftool_bin selection is tested here -- the banner previously
always checked the default binary name ("exiftool"), ignoring exif-write's
own --exiftool-bin flag, so a custom binary that WAS on PATH would still
print a misleading "not found" banner. cli.py is deliberately thin (see its
own docstring); per-subcommand argument wiring is exercised indirectly by
every other test module calling into its own `run()` directly.
"""
from flickr_unbox import cli, doctor


def _fake_check(seen):
    def check(exiftool_bin="exiftool"):
        seen.append(exiftool_bin)
        return doctor.DoctorResult(
            python_version="3.9.0", exiftool_bin=exiftool_bin, exiftool_path=None, exiftool_version=None
        )
    return check


def test_banner_uses_default_exiftool_bin_for_subcommands_without_the_flag(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli.doctor, "check", _fake_check(seen))

    cli.main(["gps-fix", "/nonexistent"])

    assert seen == ["exiftool"]
    assert "exiftool not found on PATH ('exiftool')" in capsys.readouterr().out


def test_banner_uses_custom_exiftool_bin_for_exif_write(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli.doctor, "check", _fake_check(seen))

    cli.main(["exif-write", "/nonexistent", "01", "--exiftool-bin", "/opt/custom/exiftool"])

    assert seen == ["/opt/custom/exiftool"]
    assert "/opt/custom/exiftool" in capsys.readouterr().out


def test_doctor_subcommand_itself_prints_no_banner(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli.doctor, "check", _fake_check(seen))

    cli.main(["doctor"])

    # doctor.check() is called once by _run_doctor itself, never a second
    # time for a banner (that would double-print for the one subcommand
    # that already IS the environment check).
    assert seen == ["exiftool"]
