"""Build a complete source ZIP using only Python 3.12's standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

PROJECT_NAME = "trainable-tool-agent-lab"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class SourceBundle:
    files: dict[str, bytes]
    excluded: dict[str, str]
    rules_version: int


def collect_sources(root: Path, output_dir: Path) -> SourceBundle:
    """Read allowed source files once; the ZIP and hashes use these same bytes."""
    root = root.resolve()
    output_dir = output_dir.resolve()
    if root.is_relative_to(output_dir):
        raise ValueError("The output directory cannot be the project root or its ancestor")

    with (root / "deploy/package_rules.toml").open("rb") as stream:
        rules = tomllib.load(stream)
    files: dict[str, bytes] = {}
    excluded: dict[str, str] = {}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            # Prune links before inspecting their targets, including Windows junctions.
            if path.is_symlink() or path.is_junction():
                excluded[relative] = "link"
                continue
            is_directory = path.is_dir()
            reason = None
            if path.is_relative_to(output_dir):
                reason = "output_directory"
            elif is_directory and (path / "pyvenv.cfg").exists():
                reason = "virtual_environment"
            elif is_directory and path.name.lower() in rules["exclude_directories"]:
                reason = "excluded_directory"
            elif relative != ".env.example" and any(
                fnmatchcase(path.name.lower(), pattern.lower())
                for pattern in rules["exclude_files"]
            ):
                reason = "excluded_file"
            elif not any(
                relative == entry
                or relative.startswith(entry + "/")
                or (is_directory and entry.startswith(relative + "/"))
                for entry in rules["include"]
            ):
                reason = "outside_include_scope"
            if reason:
                excluded[relative] = reason
            elif is_directory:
                visit(path)
            else:
                files[relative] = path.read_bytes()

    visit(root)
    required = set(rules["required_files"])
    for component in rules.get("required_when_present", []):
        if (root / component["directory"]).exists():
            required.update(component["files"])
    missing = sorted(required - files.keys())
    if missing:
        raise ValueError("Required files missing or excluded: " + ", ".join(missing))
    return SourceBundle(files, excluded, rules["rules_version"])


def git_metadata(root: Path) -> dict[str, str | bool | None] | None:
    """Git is optional and never determines which files are packaged."""
    executable = shutil.which("git")
    if executable is None:
        return None

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [executable, "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    if run("rev-parse", "--show-toplevel").returncode:
        return None
    head = run("rev-parse", "--verify", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=normal", "--", ".")
    return {
        "commit": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(status.stdout) if status.returncode == 0 else None,
    }


def file_records(bundle: SourceBundle) -> list[dict[str, str | int]]:
    return [
        {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in sorted(bundle.files.items())
    ]


def build_package(root: Path, output_dir: Path, bundle: SourceBundle) -> dict[str, str | int]:
    """Publish a ZIP only after all selected files and its manifest are written."""
    records = file_records(bundle)
    content_version = hashlib.sha256(
        json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    created = datetime.now(UTC)
    stamp = created.strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{PROJECT_NAME}-{stamp}-{content_version[:12]}.zip"
    manifest = {
        "manifest_version": 1,
        "project": PROJECT_NAME,
        "created_at": created.isoformat(),
        "rules_version": bundle.rules_version,
        "content_version": content_version,
        "git": git_metadata(root),
        "files": records,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / name
    checksum_path = output_dir / f"{name}.sha256"
    # Normal mkdir inherits Windows ACLs; tempfile's private ACL survives a rename.
    temporary = output_dir / f".package-{uuid4().hex}"
    temporary.mkdir()
    temporary_zip = temporary / name
    temporary_checksum = temporary / checksum_path.name
    try:
        with ZipFile(temporary_zip, "w", compression=ZIP_DEFLATED) as archive:
            for relative, content in sorted(bundle.files.items()):
                archive.writestr(f"{PROJECT_NAME}/{relative}", content)
            archive.writestr(
                f"{PROJECT_NAME}/PACKAGE_MANIFEST.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
        with temporary_zip.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        temporary_checksum.write_text(f"{digest}  {name}\n", encoding="utf-8", newline="\n")
        temporary_zip.replace(archive_path)
        temporary_checksum.replace(checksum_path)
    finally:
        temporary_zip.unlink(missing_ok=True)
        temporary_checksum.unlink(missing_ok=True)
        temporary.rmdir()

    return {
        "archive": str(archive_path),
        "checksum_file": str(checksum_path),
        "sha256": digest,
        "content_version": content_version,
        "file_count": len(records),
        "source_bytes": sum(len(content) for content in bundle.files.values()),
        "archive_bytes": archive_path.stat().st_size,
        "excluded_entries": len(bundle.excluded),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing a ZIP")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/packages"),
        help="Output directory; relative paths are resolved from the project root",
    )
    args = parser.parse_args(argv)
    output_dir = (PROJECT_ROOT / args.output_dir).resolve()
    try:
        bundle = collect_sources(PROJECT_ROOT, output_dir)
        if args.dry_run:
            result = {
                "project_root": str(PROJECT_ROOT),
                "output_dir": str(output_dir),
                "file_count": len(bundle.files),
                "source_bytes": sum(len(content) for content in bundle.files.values()),
                "included": file_records(bundle),
                "excluded": bundle.excluded,
            }
        else:
            result = build_package(PROJECT_ROOT, output_dir, bundle)
    except (OSError, ValueError) as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
