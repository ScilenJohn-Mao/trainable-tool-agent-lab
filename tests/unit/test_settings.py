import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tool_agent_lab import settings as configuration


@pytest.fixture
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / "configs").mkdir(parents=True)
    shutil.copyfile(configuration.PROJECT_ROOT / "configs/app.yaml", root / "configs/app.yaml")
    monkeypatch.setattr(configuration, "PROJECT_ROOT", root)
    for name in configuration.Settings.model_fields:
        monkeypatch.delenv(f"TTAL_{name.upper()}", raising=False)
    return root


def test_default_paths_are_separate_and_loading_has_no_writes(config_root: Path) -> None:
    settings = configuration.load_settings()
    assert settings.mode == "manual"
    assert settings.dev_owner_id == "demo-user"
    assert settings.config_version == "app-v1"
    assert settings.runtime_dir == config_root / "artifacts/runtime"
    assert settings.business_data_dir == config_root / "data/business/v1"
    assert settings.app_db_path == settings.runtime_dir / "app.sqlite3"
    assert settings.checkpoint_db_path == settings.runtime_dir / "checkpoints.sqlite3"
    assert settings.app_db_path != settings.checkpoint_db_path
    assert not settings.runtime_dir.exists()
    assert not settings.business_data_dir.exists()


def test_process_environment_overrides_dotenv_without_mutating_environment(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (config_root / ".env").write_text(
        "TTAL_MODE=mock\nTTAL_DEV_OWNER_ID=dotenv-user\nTTAL_RUNTIME_DIR=artifacts/dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TTAL_RUNTIME_DIR", "artifacts/process")
    environment_before = dict(os.environ)
    settings = configuration.load_settings()
    assert settings.mode == "mock"
    assert settings.dev_owner_id == "dotenv-user"
    assert settings.runtime_dir == config_root / "artifacts/process"
    assert dict(os.environ) == environment_before


def test_explicit_files_and_relative_paths_ignore_working_directory(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = yaml.safe_load((config_root / "configs/app.yaml").read_text(encoding="utf-8"))
    values["dev_owner_id"] = "alternate-user"
    (config_root / "configs/alternate.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    (config_root / ".env").write_text("TTAL_DEV_OWNER_ID=unused-user\n", encoding="utf-8")
    (config_root / "selected.env").write_text("TTAL_MODE=mock\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = configuration.load_settings("configs/alternate.yaml", env_file="selected.env")
    assert settings.dev_owner_id == "alternate-user"
    assert settings.mode == "mock"
    assert settings.runtime_dir == config_root / "artifacts/runtime"


def test_absolute_runtime_directory_and_explicit_initialization(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "persistent state"
    monkeypatch.setenv("TTAL_RUNTIME_DIR", str(runtime))
    settings = configuration.load_settings()
    assert settings.runtime_dir == runtime
    assert not runtime.exists()
    settings.prepare_runtime_dirs()
    assert settings.log_dir.is_dir()
    assert not settings.app_db_path.exists()
    assert not settings.checkpoint_db_path.exists()
    assert not settings.business_data_dir.exists()


@pytest.mark.parametrize("override", [{"mode": "invalid"}, {"dev_owner_id": " "}, {"runtime_path": "typo"}])
def test_invalid_configuration_is_reported(config_root: Path, override: dict[str, str]) -> None:
    path = config_root / "configs/app.yaml"
    values = yaml.safe_load(path.read_text(encoding="utf-8")) | override
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ValidationError):
        configuration.load_settings()


def test_explicit_missing_file_is_not_silently_ignored(config_root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        configuration.load_settings("configs/missing.yaml")
    with pytest.raises(FileNotFoundError):
        configuration.load_settings(env_file="missing.env")


def test_cli_inspection_and_initialization_from_another_directory(
    config_root: Path, tmp_path: Path
) -> None:
    path = config_root / "configs/app.yaml"
    values = yaml.safe_load(path.read_text(encoding="utf-8"))
    runtime = tmp_path / "cli-state"
    values["runtime_dir"] = str(runtime)
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    env_file = config_root / "empty.env"
    env_file.write_text("", encoding="utf-8")
    command = [
        sys.executable, "-m", "tool_agent_lab.settings", "--config", str(path),
        "--env-file", str(env_file),
    ]
    preview = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert json.loads(preview.stdout)["app_db_path"] == str(runtime / "app.sqlite3")
    assert not runtime.exists()
    initialized = subprocess.run(
        [*command, "--init-dirs"], cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert json.loads(initialized.stdout)["runtime_dir"] == str(runtime)
    assert (runtime / "logs").is_dir()
    assert not (runtime / "app.sqlite3").exists()
