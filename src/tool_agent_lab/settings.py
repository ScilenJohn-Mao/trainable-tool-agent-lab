"""Load application settings and inspect the paths used by future business modules."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    config_version: str = Field(min_length=1)
    mode: Literal["manual", "mock"]
    dev_owner_id: str = Field(min_length=1)
    runtime_dir: Path
    business_data_dir: Path

    @property
    def app_db_path(self) -> Path:
        """Tasks, business records and events will share this application database."""
        return self.runtime_dir / "app.sqlite3"

    @property
    def checkpoint_db_path(self) -> Path:
        return self.runtime_dir / "checkpoints.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.runtime_dir / "logs"

    def prepare_runtime_dirs(self) -> None:
        """Create runtime directories only; database initialization belongs to M1-008."""
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def summary(self) -> dict[str, str]:
        return self.model_dump(mode="json") | {
            "app_db_path": str(self.app_db_path),
            "checkpoint_db_path": str(self.checkpoint_db_path),
            "log_dir": str(self.log_dir),
        }


def _project_path(path: str | Path) -> Path:
    return (PROJECT_ROOT / Path(path).expanduser()).resolve()


def load_settings(
    config_path: str | Path | None = None,
    *,
    env_file: str | Path | None = None,
) -> Settings:
    """Read YAML, optional dotenv and process overrides without changing os.environ."""
    config = _project_path(config_path if config_path is not None else "configs/app.yaml")
    with config.open(encoding="utf-8") as stream:
        values = yaml.safe_load(stream)
    if not isinstance(values, dict):
        raise ValueError(f"Application configuration must be a YAML mapping: {config}")

    dotenv_path = _project_path(env_file if env_file is not None else ".env")
    overrides = {}
    if env_file is not None or dotenv_path.exists():
        with dotenv_path.open(encoding="utf-8") as stream:
            overrides.update(dotenv_values(stream=stream, interpolate=False))
    overrides.update(os.environ)
    for field in Settings.model_fields:
        variable = f"TTAL_{field.upper()}"
        if variable in overrides:
            values[field] = overrides[variable]

    settings = Settings.model_validate(values)
    return settings.model_copy(update={
        "runtime_dir": _project_path(settings.runtime_dir),
        "business_data_dir": _project_path(settings.business_data_dir),
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="YAML file; defaults to configs/app.yaml")
    parser.add_argument("--env-file", type=Path, help="Override the optional project .env file")
    parser.add_argument("--init-dirs", action="store_true", help="Create runtime and log directories")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config, env_file=args.env_file)
        if args.init_dirs:
            settings.prepare_runtime_dirs()
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    print(json.dumps(settings.summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
