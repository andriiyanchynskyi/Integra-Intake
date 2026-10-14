from pathlib import Path
import zipfile

import pytest

from scripts.verify_release_artifact import (
    build_release_archive,
    verify_release_archive,
)


def test_release_archive_contains_only_allowlisted_release_files(tmp_path: Path) -> None:
    archive_path = build_release_archive(tmp_path / "release.zip")

    members = verify_release_archive(archive_path)

    assert "pyproject.toml" in members
    assert "Dockerfile" in members
    assert "docs/runbooks/live-provider-demo.md" in members
    assert all(".env" not in member or member == ".env.example" for member in members)
    assert not any("__pycache__" in member or member.endswith(".pyc") for member in members)


def test_release_archive_rejects_secret_or_traversal_member(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("pyproject.toml", "[project]\nname='x'\n")
        archive.writestr("../.env", "SECRET=bad")

    with pytest.raises(ValueError, match="unsafe member"):
        verify_release_archive(archive_path)
