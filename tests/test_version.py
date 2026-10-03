"""Tests for version.py."""
from pathlib import Path

from version import get_version

REPO_VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()


def test_get_version_reads_repo_version_file():
    assert get_version() == REPO_VERSION


def test_get_version_prefers_first_existing_candidate(monkeypatch, tmp_path):
    import version

    good = tmp_path / "VERSION"
    good.write_text("9.9.9\n")
    monkeypatch.setattr(version, "VERSION_CANDIDATES", [tmp_path / "missing", good])
    assert version.get_version() == "9.9.9"


def test_get_version_falls_back_when_nothing_found(monkeypatch, tmp_path):
    import version

    monkeypatch.setattr(version, "VERSION_CANDIDATES", [tmp_path / "missing"])
    assert version.get_version() == "1.0.0"


def test_module_level_version_constant_matches_file():
    import version

    assert version.VERSION == REPO_VERSION
