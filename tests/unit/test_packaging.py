"""Check the contents and usability of delivered source archives."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest

from scripts import package_project as packaging


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source project"
    rules = tomllib.loads(
        (packaging.PROJECT_ROOT / "deploy/package_rules.toml").read_text(encoding="utf-8")
    )
    for relative in rules["required_files"]:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(packaging.PROJECT_ROOT / relative, destination)
    return root


def run_cli(root: Path, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    # -I -S proves the packaging CLI does not need project installation or site packages.
    return subprocess.run(
        [sys.executable, "-I", "-S", str(root / "scripts/package_project.py"), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_resources_retained_and_environment_runtime_files_excluded(source_tree: Path) -> None:
    retained = {
        ".env.example": "MODEL_API_KEY=\n",
        "src/tool_agent_lab/new_uncommitted.py": "VALUE = 1\n",
        "src/tool_agent_lab/storage/migrations/001_initial.sql": "SELECT 1;\n",
        "configs/models/base.yaml": "model: example\n",
        "data/business/v1/policies.json": "[]\n",
        "frontend/package.json": '{"name":"fixture"}\n',
        "frontend/package-lock.json": '{"lockfileVersion":3}\n',
        "frontend/src/main.tsx": "export {};\n",
        "training/art/pyproject.toml": '# Test fixture, not a training environment.\n',
        "training/art/uv.lock": '# Test fixture, not a generated training lock.\n',
    }
    excluded = {
        "src/.venv/hidden.py": "ignore",
        "src/nested/custom-python/pyvenv.cfg": "home = unused",
        "src/nested/custom-python/Lib/hidden.py": "ignore",
        "src/__pycache__/cached.pyc": "ignore",
        "src/tool_agent_lab/.uv-cache/cache.json": "ignore",
        "src/tool_agent_lab/.pytest_cache/result": "ignore",
        "frontend/node_modules/example/index.js": "ignore",
        "frontend/dist/index.html": "ignore",
        "frontend/.env.example": "nested examples are not explicitly allowed",
        ".env": "not-real-secrets",
        "configs/.env.production": "not-real-secrets",
        "configs/app.local.yaml": "ignore",
        "configs/private.pem": "ignore",
        "configs/models/adapter.safetensors": "ignore",
        "data/business/runtime.sqlite3": "ignore",
        "data/business/runtime.sqlite3-wal": "ignore",
        "data/business/runtime.sqlite3-shm": "ignore",
        "data/business/runtime.log": "ignore",
        "training/art/custom-env/pyvenv.cfg": "home = unused",
        "training/art/checkpoints/step-1/config.json": "ignore",
        "scripts/previous.zip": "ignore",
        "scripts/previous.zip.sha256": "ignore",
        "scripts/old.tar.gz": "ignore",
        "scripts/.git/config": "ignore",
        "artifacts/generated.json": "ignore",
        "reports/progress.md": "ignore",
        "reports/acceptance.md": "ignore",
        "docs/AGENTS.md": "ignore",
        "src/.codex/config.toml": "ignore",
        "ART/unrelated.py": "ignore",
        "research.md": "ignore",
    }
    write_files(source_tree, retained | excluded)
    bundle = packaging.collect_sources(source_tree, source_tree / "artifacts/packages")

    assert retained.keys() <= bundle.files.keys()
    assert not (excluded.keys() & bundle.files.keys())
    assert bundle.excluded["src/nested/custom-python"] == "virtual_environment"
    assert bundle.files["configs/models/base.yaml"] == b"model: example\n"
    assert not (source_tree / ".git").exists()


def test_archive_manifest_hashes_and_extracted_cli(
    source_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(packaging.shutil, "which", lambda name: None)
    write_files(source_tree, {"src/tool_agent_lab/new_file.py": "VALUE = 42\n"})
    output = source_tree / "artifacts/packages"
    bundle = packaging.collect_sources(source_tree, output)
    result = packaging.build_package(source_tree, output, bundle)
    archive_path = Path(result["archive"])
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == result["sha256"]
    assert Path(result["checksum_file"]).read_bytes() == (
        f"{result['sha256']}  {archive_path.name}\n".encode()
    )

    with ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        manifest_path = f"{packaging.PROJECT_NAME}/PACKAGE_MANIFEST.json"
        manifest = json.loads(archive.read(manifest_path))
        assert manifest["git"] is None
        assert manifest["content_version"] == result["content_version"]
        assert manifest["rules_version"] == 1
        assert len(manifest["files"]) == len(bundle.files) == result["file_count"]
        expected = {f"{packaging.PROJECT_NAME}/{name}" for name in bundle.files}
        assert set(archive.namelist()) == expected | {manifest_path}
        for name in archive.namelist():
            path = PurePosixPath(name)
            assert not path.is_absolute() and ".." not in path.parts and "\\" not in name
        for record in manifest["files"]:
            content = archive.read(f"{packaging.PROJECT_NAME}/{record['path']}")
            assert len(content) == record["size"]
            assert hashlib.sha256(content).hexdigest() == record["sha256"]
            assert content == bundle.files[record["path"]]
        archive.extractall(tmp_path / "release")

    extracted = tmp_path / "release" / packaging.PROJECT_NAME
    assert set((tmp_path / "release").iterdir()) == {extracted}
    command = run_cli(extracted, tmp_path, "--dry-run")
    assert command.returncode == 0, command.stderr
    preview = json.loads(command.stdout)
    assert {record["path"] for record in preview["included"]} == bundle.files.keys()
    assert preview["excluded"]["PACKAGE_MANIFEST.json"] == "outside_include_scope"


def test_dry_run_and_custom_output_are_independent_of_working_directory(
    source_tree: Path, tmp_path: Path
) -> None:
    output = source_tree / "scripts/delivery"
    first = run_cli(source_tree, source_tree, "--dry-run", "--output-dir", "scripts/delivery")
    second = run_cli(source_tree, tmp_path, "--dry-run", "--output-dir", "scripts/delivery")
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert not output.exists()

    write_files(output, {"old.py": "this output must not enter the archive"})
    for _ in range(2):
        command = run_cli(source_tree, tmp_path, "--output-dir", "scripts/delivery")
        assert command.returncode == 0, command.stderr
        result = json.loads(command.stdout)
        assert Path(result["archive"]).parent == output
        with ZipFile(result["archive"]) as archive:
            assert not any("/scripts/delivery/" in name for name in archive.namelist())
    assert len(list(output.glob("*.zip"))) == 2


@pytest.mark.parametrize(
    "missing", ["uv.lock", "frontend/package-lock.json", "training/art/uv.lock"]
)
def test_missing_required_lock_fails_without_creating_archive(source_tree: Path, missing: str) -> None:
    if missing == "uv.lock":
        (source_tree / missing).unlink()
    else:
        write_files(source_tree, {str(Path(missing).parent / "started.txt"): "started"})
    command = run_cli(source_tree, source_tree)
    assert command.returncode != 0
    assert missing in command.stderr
    assert not (source_tree / "artifacts/packages").exists()


def test_output_cannot_hide_project_root(source_tree: Path) -> None:
    command = run_cli(source_tree, source_tree, "--output-dir", ".")
    assert command.returncode != 0
    assert "cannot be the project root" in command.stderr


def test_read_failure_does_not_publish_incomplete_package(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path.name == "uv.lock":
            raise PermissionError("cannot read uv.lock")
        return original(path)

    monkeypatch.setattr(packaging, "PROJECT_ROOT", source_tree)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert packaging.main([]) == 1
    assert "cannot read uv.lock" in capsys.readouterr().err
    assert not (source_tree / "artifacts/packages").exists()


def test_write_failure_does_not_publish_incomplete_package(
    source_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = ZipFile.writestr

    def fail_manifest(archive: ZipFile, name: str, content: bytes | str) -> None:
        if name.endswith("/PACKAGE_MANIFEST.json"):
            raise OSError("cannot finish manifest")
        original(archive, name, content)

    monkeypatch.setattr(packaging, "PROJECT_ROOT", source_tree)
    monkeypatch.setattr(ZipFile, "writestr", fail_manifest)
    assert packaging.main([]) == 1
    assert "cannot finish manifest" in capsys.readouterr().err
    assert list((source_tree / "artifacts/packages").iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL inheritance regression")
def test_delivery_files_inherit_output_read_permissions(source_tree: Path) -> None:
    output = source_tree / "artifacts/packages"
    output.mkdir(parents=True)
    environment = {**os.environ, "TEST_PACKAGE_OUTPUT": str(output)}
    # Give an ordinary reader access to this test directory independently of its owner.
    subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference = 'Stop'\n"
            "$acl = Get-Acl -LiteralPath $env:TEST_PACKAGE_OUTPUT\n"
            "$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')\n"
            "$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(\n"
            "  $sid, 'ReadAndExecute', 'ContainerInherit, ObjectInherit', 'None', 'Allow')\n"
            "$acl.AddAccessRule($rule)\n"
            "Set-Acl -LiteralPath $env:TEST_PACKAGE_OUTPUT -AclObject $acl",
        ],
        env=environment, capture_output=True, check=True,
    )
    bundle = packaging.collect_sources(source_tree, output)
    packaging.build_package(source_tree, output, bundle)
    command = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference = 'Stop'\n"
            "$records = @(Get-ChildItem -LiteralPath $env:TEST_PACKAGE_OUTPUT -File | "
            "ForEach-Object {\n"
            "  $acl = Get-Acl -LiteralPath $_.FullName\n"
            "  $readers = @($acl.GetAccessRules($true, $true, "
            "[System.Security.Principal.SecurityIdentifier]) | Where-Object {\n"
            "    $_.IdentityReference.Value -eq 'S-1-5-32-545' -and $_.IsInherited -and\n"
            "    $_.AccessControlType -eq 'Allow' -and\n"
            "    ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Read) "
            "-eq [System.Security.AccessControl.FileSystemRights]::Read\n"
            "  })\n"
            "  [PSCustomObject]@{\n"
            "    file = $_.Name\n"
            "    inheritance_enabled = -not $acl.AreAccessRulesProtected\n"
            "    ordinary_users_can_read = $readers.Count -gt 0\n"
            "  }\n"
            "})\n"
            "ConvertTo-Json -InputObject $records -Compress",
        ],
        env=environment, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    records = json.loads(command.stdout)
    assert len(records) == 2
    assert all(record["inheritance_enabled"] for record in records), records
    assert all(record["ordinary_users_can_read"] for record in records), records


def test_external_directory_link_is_not_followed(source_tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write_files(outside, {"external.py": "must remain outside"})
    link = source_tree / "src/external"
    if os.name == "nt":
        subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_PACKAGE_LINK "
                "-Target $env:TEST_PACKAGE_TARGET | Out-Null",
            ],
            env={**os.environ, "TEST_PACKAGE_LINK": str(link), "TEST_PACKAGE_TARGET": str(outside)},
            capture_output=True,
            check=True,
        )
        assert link.is_junction()
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        bundle = packaging.collect_sources(source_tree, source_tree / "artifacts/packages")
        assert bundle.excluded["src/external"] == "link"
        assert not any(name.startswith("src/external/") for name in bundle.files)
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove only the junction; never recursively remove its target.
        else:
            link.unlink()
    assert (outside / "external.py").read_text() == "must remain outside"


def test_uncommitted_files_in_git_repository_are_included(source_tree: Path) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is optional; the non-Git archive test still applies")
    subprocess.run([executable, "init", "--quiet", str(source_tree)], check=True)
    write_files(source_tree, {"src/new_untracked.py": "VALUE = 7\n"})
    bundle = packaging.collect_sources(source_tree, source_tree / "artifacts/packages")
    assert "src/new_untracked.py" in bundle.files
    assert not any(name.startswith(".git/") for name in bundle.files)
    metadata = packaging.git_metadata(source_tree)
    assert metadata == {"commit": None, "dirty": True}
