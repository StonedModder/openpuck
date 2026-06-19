from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


def _runtime_app_root() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() == "dist" else exe_dir
    return Path(__file__).resolve().parents[3]


def _discover_repo_root() -> Path:
    base = _runtime_app_root()
    candidates = [
        base / "openpuck",
        base.parent / "openpuck",
        Path.cwd() / "openpuck",
        Path.cwd().parent / "openpuck",
    ]
    for candidate in candidates:
        if (candidate / "docs" / "BUILD_AND_DEPLOY.md").exists() and (candidate / "OpenPuck").exists():
            return candidate.resolve()
    return (base / "openpuck").resolve()


def _default_repo_root() -> Path:
    return _discover_repo_root()


def _default_workspace_root() -> Path:
    return _runtime_app_root()


def _default_config_path() -> Path:
    appdata = os.getenv("APPDATA")
    if platform.system() == "Windows" and appdata:
        return Path(appdata) / "OpenPuckFlasher" / "settings.json"
    return Path.home() / ".openpuck-flasher.json"


@dataclass(slots=True)
class AppConfig:
    repo_root: str = str(_default_repo_root())
    build_root: str = str(_default_workspace_root() / "openpuck-flasher" / "artifacts")
    arduino_cli_path: str = "arduino-cli"
    python_path: str = "py" if platform.system() == "Windows" else "python3"
    adafruit_nrfutil_path: str = "adafruit-nrfutil"
    serial_baud: int = 115200
    board_fqbn: str = "adafruit:nrf52:feather52840"
    panel_port: int = 8008
    update_source: str = "https://api.github.com/repos/safijari/openpuck/commits/main"
    check_for_updates: bool = True
    auto_refresh_devices: bool = True
    theme_mode: str = "dark"
    ui_scaling: float = 1.0
    tooltip_reference: str = "docs/BUILD_AND_DEPLOY.md"


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_config_path()

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        config = AppConfig(**data)
        expected_repo = _discover_repo_root()
        if not (Path(config.repo_root) / "docs" / "BUILD_AND_DEPLOY.md").exists():
            config.repo_root = str(expected_repo)
        if not config.build_root or "AppData\\Local\\openpuck" in config.build_root:
            config.build_root = str(_default_workspace_root() / "artifacts")
        return config

    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(config), indent=2),
            encoding="utf-8",
        )
