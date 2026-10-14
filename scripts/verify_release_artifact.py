"""Build and verify the allowlisted IntegraIntake release archive."""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


REQUIRED_FILES = frozenset(
    {
        "pyproject.toml",
        "Dockerfile",
        "alembic.ini",
        ".github/workflows/ci.yml",
        ".gitattributes",
        "docs/runbooks/live-provider-demo.md",
        ".env.example",
    }
)
REQUIRED_ROOTS = frozenset({"app/", "alembic/", "examples/", "tests/"})
ALLOWED_ROOTS = (
    "app",
    "alembic",
    "examples",
    "evals",
    "tests",
    "scripts",
    "docs/runbooks",
    ".github/workflows",
)
ALLOWED_FILES = frozenset(
    {
        "README.md",
        "LICENSE",
        "Dockerfile",
        "alembic.ini",
        "docker-compose.yml",
        ".env.example",
        ".gitattributes",
        "pyproject.toml",
    }
)
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        ".superpowers",
    }
)
FORBIDDEN_SUFFIXES = (".pyc", ".pyo")
FORBIDDEN_NAMES = frozenset({".env", "secrets.json", "credentials.json"})


def _is_forbidden(path: str) -> bool:
    parts = Path(path).parts
    return (
        any(part in FORBIDDEN_PARTS for part in parts)
        or Path(path).name in FORBIDDEN_NAMES
        or Path(path).suffix in FORBIDDEN_SUFFIXES
    )


def _allowlisted_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for relative in sorted(ALLOWED_FILES):
        path = root / relative
        if path.is_file():
            paths.append(path)
    for directory in ALLOWED_ROOTS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                name = path.relative_to(root).as_posix()
                if not _is_forbidden(name):
                    paths.append(path)
    unique = {path.resolve(): path for path in paths}
    return tuple(unique[path] for path in sorted(unique))


def _validate_members(members: set[str]) -> None:
    if any(
        not name
        or name.startswith("/")
        or "\\" in name
        or ".." in Path(name).parts
        or _is_forbidden(name)
        for name in members
    ):
        raise ValueError("release archive contains an unsafe member")
    missing = sorted(REQUIRED_FILES - members)
    missing.extend(
        root for root in sorted(REQUIRED_ROOTS) if not any(name.startswith(root) for name in members)
    )
    if missing:
        raise ValueError("release archive is missing: " + ", ".join(missing))


def build_release_archive(
    output: Path,
    *,
    root: Path | None = None,
) -> Path:
    """Create one source archive from the explicit repository allowlist."""

    repository = (root or Path(__file__).resolve().parents[1]).resolve()
    files = _allowlisted_paths(repository)
    members = {path.relative_to(repository).as_posix() for path in files}
    _validate_members(members)
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(repository).as_posix())
    return destination


def verify_release_archive(archive_path: Path) -> tuple[str, ...]:
    """Validate an existing release archive without reading secret files."""

    with zipfile.ZipFile(archive_path) as archive:
        members = {name for name in archive.namelist() if not name.endswith("/")}
    _validate_members(members)
    return tuple(sorted(members))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist") / "integra-intake-release.zip",
    )
    parser.add_argument("--verify", type=Path, help="verify an existing archive")
    args = parser.parse_args()
    archive = args.verify or build_release_archive(args.output)
    verify_release_archive(archive)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
