from __future__ import annotations

import os
import shutil
import site
import subprocess
import sysconfig
from sys import version_info
from pathlib import Path
from typing import Callable

from ..config import AppConfig
from ..models import BuildArtifacts
from .command_runner import CommandRunner


LogFn = Callable[[str], None]


class OpenPuckService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.runner = CommandRunner()

    @property
    def repo_root(self) -> Path:
        return Path(self.config.repo_root)

    @property
    def docs_root(self) -> Path:
        return self.repo_root / "docs"

    @property
    def build_root(self) -> Path:
        return Path(self.config.build_root)

    def ensure_prerequisites(self, log: LogFn) -> None:
        self._check_tool(self.config.arduino_cli_path, ["version"], log, "arduino-cli")
        self._check_tool(self.config.python_path, ["--version"], log, "Python")
        self._check_tool(*self._nrfutil_check_command(), log, "adafruit-nrfutil", optional=True)

    def install_core(self, log: LogFn) -> None:
        self.runner.run([self.config.python_path, "-m", "pip", "install", "adafruit-nrfutil"], log=log)
        self.runner.run([self.config.arduino_cli_path, "config", "init"], log=log)
        self.runner.run([self.config.arduino_cli_path, "core", "update-index"], log=log)
        self.runner.run(
            [
                self.config.arduino_cli_path,
                "core",
                "install",
                "adafruit:nrf52",
                "--additional-urls",
                "https://adafruit.github.io/arduino-board-index/package_adafruit_index.json",
            ],
            log=log,
        )

    def build_firmware(self, *, factory_reset: bool, log: LogFn) -> BuildArtifacts:
        self.ensure_prerequisites(log)
        self._validate_repo_root(log)
        build_dir = self.build_root / ("factory-reset" if factory_reset else "standard")
        build_dir.mkdir(parents=True, exist_ok=True)
        self._generate_version_header(log)
        flags = "-DNRF52840_XXAA {build.flags.usb} -DCFG_TUD_HID=4"
        if factory_reset:
            flags += " -DOPK_FACTORY_RESET=1"
        self.runner.run(
            [
                self.config.arduino_cli_path,
                "compile",
                "-b",
                self.config.board_fqbn,
                "--build-property",
                f"build.extra_flags={flags}",
                "--build-path",
                str(build_dir),
                "OpenPuck",
            ],
            cwd=self.repo_root,
            log=log,
        )
        artifacts = BuildArtifacts(
            build_dir=build_dir,
            uf2_path=self._find_first(build_dir, "*.uf2"),
            zip_path=self._find_first(build_dir, "*.zip"),
            bin_path=self._find_first(build_dir, "*.bin"),
            header_path=self.repo_root / "OpenPuck" / "git_version.h",
        )
        if not artifacts.uf2_path:
            log("No .uf2 artifact was produced by the current board core; DFU .zip is available.")
        log(f"Artifacts: UF2={artifacts.uf2_path} ZIP={artifacts.zip_path} BIN={artifacts.bin_path}")
        return artifacts

    def discover_existing_artifacts(self) -> BuildArtifacts | None:
        for folder_name in ("standard", "factory-reset"):
            build_dir = self.build_root / folder_name
            if not build_dir.exists():
                continue
            artifacts = BuildArtifacts(
                build_dir=build_dir,
                uf2_path=self._find_first(build_dir, "*.uf2"),
                zip_path=self._find_first(build_dir, "*.zip"),
                bin_path=self._find_first(build_dir, "*.bin"),
                header_path=self.repo_root / "OpenPuck" / "git_version.h",
            )
            if artifacts.uf2_path or artifacts.zip_path or artifacts.bin_path:
                return artifacts
        return None

    def upload_serial(self, port: str, build_dir: Path, log: LogFn) -> None:
        self._validate_repo_root(log)
        if not build_dir.exists():
            raise RuntimeError(f"Build directory not found: {build_dir}. Build firmware first.")
        self.runner.run(
            [
                self.config.arduino_cli_path,
                "upload",
                "-b",
                self.config.board_fqbn,
                "-p",
                port,
                "--build-path",
                str(build_dir),
                "OpenPuck",
            ],
            cwd=self.repo_root,
            log=log,
        )

    def flash_dfu_serial(self, port: str, package_path: Path, log: LogFn) -> None:
        self._validate_repo_root(log)
        self.runner.run(
            [
                *self._nrfutil_exec_command(),
                "--verbose",
                "dfu",
                "serial",
                "--package",
                str(package_path),
                "-p",
                port,
                "-b",
                "115200",
            ],
            cwd=self.repo_root,
            log=log,
        )

    def copy_uf2(self, uf2_path: Path, target_dir: Path, log: LogFn) -> Path:
        if not target_dir.exists():
            raise FileNotFoundError(f"UF2 target not found: {target_dir}")
        destination = target_dir / uf2_path.name
        shutil.copy2(uf2_path, destination)
        log(f"Copied {uf2_path} -> {destination}")
        return destination

    def erase_all_serial(self, port: str, log: LogFn) -> None:
        import serial

        with serial.Serial(port, self.config.serial_baud, timeout=2) as handle:
            handle.write(b"ERASE-ALL\r\n")
            handle.flush()
        log(f"Sent ERASE-ALL to {port}")

    def launch_installer_hint(self) -> str:
        if os.name == "nt":
            return "Install arduino-cli with Chocolatey or the Arduino MSI, then put it on PATH."
        return "Install arduino-cli from your package manager or Arduino release archive and retry."

    def _check_tool(
        self,
        tool: str,
        args: list[str],
        log: LogFn,
        label: str,
        *,
        optional: bool = False,
    ) -> None:
        try:
            self.runner.run([tool, *args], log=log)
        except Exception as exc:
            if optional:
                log(f"{label} check skipped: {exc}")
                return
            raise RuntimeError(f"{label} is required. {self.launch_installer_hint()}") from exc

    def _nrfutil_check_command(self) -> tuple[str, list[str]]:
        candidate = self._find_nrfutil_executable()
        if candidate:
            return candidate, ["version"]
        return self.config.python_path, ["-m", "nordicsemi", "version"]

    def _nrfutil_exec_command(self) -> list[str]:
        candidate = self._find_nrfutil_executable()
        if candidate:
            return [candidate]
        return [self.config.python_path, "-m", "nordicsemi"]

    def _find_nrfutil_executable(self) -> str | None:
        configured = self.config.adafruit_nrfutil_path
        direct = shutil.which(configured)
        if direct:
            return direct
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
        script_dir = Path(sysconfig.get_path("scripts"))
        userbase = Path(site.getuserbase())
        if os.name == "nt":
            versioned_user_scripts = userbase / f"Python{version_info.major}{version_info.minor}" / "Scripts"
            user_script_dir = userbase / "Scripts"
            search_dirs = [script_dir, user_script_dir, versioned_user_scripts]
        else:
            user_script_dir = userbase / "bin"
            search_dirs = [script_dir, user_script_dir]
        names = ["adafruit-nrfutil.exe", "adafruit-nrfutil"]
        for name in names:
            for folder in search_dirs:
                candidate = folder / name
                if candidate.exists():
                    return str(candidate)
        return None

    def _find_first(self, root: Path, pattern: str) -> Path | None:
        matches = sorted(root.rglob(pattern))
        return matches[0] if matches else None

    def _validate_repo_root(self, log: LogFn) -> None:
        docs = self.repo_root / "docs" / "BUILD_AND_DEPLOY.md"
        sketch = self.repo_root / "OpenPuck"
        if docs.exists() and sketch.exists():
            return
        raise RuntimeError(
            f"OpenPuck repo not found at {self.repo_root}. Set the Repository Root in Settings to your cloned openpuck folder."
        )

    def _generate_version_header(self, log: LogFn) -> None:
        script = self.repo_root / "gen_version.sh"
        if shutil.which("bash"):
            self.runner.run(["bash", str(script)], cwd=self.repo_root, log=log)
            return
        if shutil.which("sh"):
            self.runner.run(["sh", str(script)], cwd=self.repo_root, log=log)
            return
        hash_text = subprocess.check_output(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=self.repo_root,
            text=True,
            encoding="utf-8",
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=self.repo_root,
            text=True,
            encoding="utf-8",
        ).strip()
        header = self.repo_root / "OpenPuck" / "git_version.h"
        header.write_text(
            "\n".join(
                [
                    "// AUTO-GENERATED by openpuck-flasher fallback.",
                    "#pragma once",
                    f'#define OPK_GIT_HASH "{hash_text}"',
                    f"#define OPK_GIT_DIRTY {1 if dirty else 0}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        log(f"Generated {header} via Python fallback")
