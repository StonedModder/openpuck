from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PortRecord:
    device: str
    label: str
    board_name: str = ""
    fqbn: str = ""
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    is_supported: bool = False
    is_openpuck: bool = False
    is_bootloader: bool = False
    openpuck_mode: str | None = None
    openpuck_build: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        badges: list[str] = []
        if self.is_supported:
            badges.append("supported")
        if self.is_openpuck:
            badges.append("openpuck")
        if self.is_bootloader:
            badges.append("bootloader")
        suffix = f" [{' | '.join(badges)}]" if badges else ""
        return f"{self.device} - {self.label}{suffix}"


@dataclass(slots=True)
class BuildArtifacts:
    build_dir: Path
    uf2_path: Path | None = None
    zip_path: Path | None = None
    bin_path: Path | None = None
    header_path: Path | None = None


@dataclass(slots=True)
class UpdateInfo:
    current: str
    latest: str
    source: str
    url: str
    available: bool
