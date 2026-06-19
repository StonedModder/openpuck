from __future__ import annotations

import json
import os
import re
from pathlib import Path

from serial.tools import list_ports

from ..config import AppConfig
from ..models import PortRecord
from .command_runner import CommandRunner
from .webusb import OPENPUCK_USB_IDS, WebUsbProbe

ADAFRUIT_USB_VID = 0x239A
ADAFRUIT_BOOTLOADER_SERIAL_PIDS = {
    0x00B3,  # Common clone/bootloader serial DFU port observed on Windows
}


class DeviceService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.runner = CommandRunner()
        self.webusb = WebUsbProbe()
        self._cache: dict[str, PortRecord] = {}

    def detect(self, *, include_cli_probe: bool = True) -> list[PortRecord]:
        ports = {port.device: self._from_serial_port(port) for port in list_ports.comports()}
        if include_cli_probe:
            cli_rows = self._arduino_board_list()
            for row in cli_rows:
                port_name = row.get("address") or row.get("port", {}).get("address")
                if not port_name:
                    continue
                record = ports.setdefault(
                    port_name,
                    PortRecord(device=port_name, label=row.get("name", "Unknown device")),
                )
                self._apply_cli_port_properties(record, row)
                detected_boards = row.get("matching_boards") or []
                if detected_boards:
                    board = detected_boards[0]
                    record.board_name = board.get("name", "")
                    record.fqbn = board.get("fqbn", "")
                if not record.label or record.label == "Unknown":
                    record.label = row.get("name", record.label)
                if "nrf52840" in (record.board_name + record.label + record.fqbn).lower():
                    record.is_supported = True
        self._merge_windows_usb_devices(ports)

        status = self.webusb.read_openpuck_status()
        if status:
            matched = False
            for record in ports.values():
                if record.vid == 0x28DE and record.pid == 0x1142:
                    matched = True
                    record.is_openpuck = True
                    record.openpuck_mode = status.mode_name
                    record.openpuck_build = status.git_hash
                    if status.dirty and status.git_hash:
                        record.openpuck_build = f"{status.git_hash} (dirty)"
            if not matched:
                pseudo = PortRecord(
                    device="USB:28DE:1142",
                    label="OpenPuck WebUSB device",
                    vid=0x28DE,
                    pid=0x1142,
                    is_openpuck=True,
                    openpuck_mode=status.mode_name,
                    openpuck_build=status.git_hash,
                )
                if status.dirty and status.git_hash:
                    pseudo.openpuck_build = f"{status.git_hash} (dirty)"
                pseudo.notes.append("Detected over WebUSB without a serial port")
                ports[pseudo.device] = pseudo
        self._merge_cached_metadata(ports, include_cli_probe=include_cli_probe)
        return sorted(
            ports.values(),
            key=lambda item: (not item.is_openpuck, not item.is_supported, item.device),
        )

    def _from_serial_port(self, port: list_ports.ListPortInfo) -> PortRecord:
        record = PortRecord(
            device=port.device,
            label=port.description or port.name or "Unknown",
            vid=port.vid,
            pid=port.pid,
            serial_number=port.serial_number,
        )
        self._classify_bootloader(record)
        usb_mode = OPENPUCK_USB_IDS.get((port.vid, port.pid))
        if usb_mode:
            record.is_openpuck = True
            record.openpuck_mode = usb_mode
            record.notes.append(f"USB identity matches OpenPuck {usb_mode} mode")
        haystack = " ".join(
            part for part in [port.manufacturer, port.product, port.description] if part
        ).lower()
        if "nrf52840" in haystack or "feather" in haystack or "pro micro" in haystack:
            record.is_supported = True
        return record

    def _apply_cli_port_properties(self, record: PortRecord, row: dict) -> None:
        port = row.get("port", {})
        props = port.get("properties") or {}
        vid = self._parse_int(props.get("vid"))
        pid = self._parse_int(props.get("pid"))
        if vid is not None and record.vid is None:
            record.vid = vid
        if pid is not None and record.pid is None:
            record.pid = pid
        serial_number = props.get("serialNumber")
        if serial_number and not record.serial_number:
            record.serial_number = serial_number
        if port.get("protocol") == "serial" and port.get("protocol_label"):
            record.notes.append(f"Arduino CLI reports {port['protocol_label']}")
        self._classify_bootloader(record)

    def _arduino_board_list(self) -> list[dict]:
        try:
            output = self.runner.capture(
                [self.config.arduino_cli_path, "board", "list", "--format", "json"],
            ).strip()
        except Exception:
            return []
        if not output:
            return []
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return parsed
        return parsed.get("detected_ports", [])

    def _merge_windows_usb_devices(self, ports: dict[str, PortRecord]) -> None:
        if os.name != "nt":
            return
        devices = self._windows_usb_devices()
        for device in devices:
            instance_id = device.get("InstanceId", "")
            friendly_name = device.get("FriendlyName") or device.get("Class") or "USB device"
            match = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", instance_id, re.IGNORECASE)
            if not match:
                continue
            vid = int(match.group(1), 16)
            pid = int(match.group(2), 16)
            mode = OPENPUCK_USB_IDS.get((vid, pid))
            if not mode:
                continue
            pseudo_id = f"USB:{vid:04X}:{pid:04X}"
            record = ports.get(pseudo_id)
            if not record:
                record = PortRecord(
                    device=pseudo_id,
                    label=friendly_name,
                    vid=vid,
                    pid=pid,
                )
                ports[pseudo_id] = record
            record.is_openpuck = True
            record.openpuck_mode = mode
            if mode == "Steam":
                record.notes.append("OpenPuck Steam USB identity detected")
            else:
                record.notes.append(f"OpenPuck USB identity detected in {mode} mode")

    def _windows_usb_devices(self) -> list[dict]:
        patterns = "|".join(
            [f"VID_{vid:04X}&PID_{pid:04X}" for (vid, pid) in OPENPUCK_USB_IDS.keys()]
        )
        command = (
            "$matches = Get-PnpDevice -PresentOnly | "
            f"Where-Object {{ $_.InstanceId -match '{patterns}' }} | "
            "Select-Object FriendlyName,InstanceId,Class,Status; "
            "if ($matches) { $matches | ConvertTo-Json -Compress }"
        )
        try:
            output = self.runner.capture(
                ["powershell", "-NoProfile", "-Command", command],
            ).strip()
        except Exception:
            return []
        if not output:
            return []
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else [parsed]

    def _merge_cached_metadata(
        self,
        ports: dict[str, PortRecord],
        *,
        include_cli_probe: bool,
    ) -> None:
        for device, record in ports.items():
            cached = self._cache.get(device)
            if cached and not include_cli_probe:
                if not record.board_name:
                    record.board_name = cached.board_name
                if not record.fqbn:
                    record.fqbn = cached.fqbn
                if cached.is_supported:
                    record.is_supported = True
                if cached.is_openpuck:
                    record.is_openpuck = True
                if cached.is_bootloader:
                    record.is_bootloader = True
                if not record.openpuck_mode:
                    record.openpuck_mode = cached.openpuck_mode
                if not record.openpuck_build:
                    record.openpuck_build = cached.openpuck_build
                for note in cached.notes:
                    if note not in record.notes:
                        record.notes.append(note)
            self._cache[device] = PortRecord(
                device=record.device,
                label=record.label,
                board_name=record.board_name,
                fqbn=record.fqbn,
                vid=record.vid,
                pid=record.pid,
                serial_number=record.serial_number,
                is_supported=record.is_supported,
                is_openpuck=record.is_openpuck,
                is_bootloader=record.is_bootloader,
                openpuck_mode=record.openpuck_mode,
                openpuck_build=record.openpuck_build,
                notes=list(record.notes),
            )

    def _classify_bootloader(self, record: PortRecord) -> None:
        haystack = " ".join(part for part in [record.label, record.board_name, record.fqbn] if part).lower()
        if record.vid == ADAFRUIT_USB_VID and record.pid in ADAFRUIT_BOOTLOADER_SERIAL_PIDS:
            record.is_bootloader = True
            record.is_supported = True
            if "Adafruit nRF52 serial DFU bootloader" not in record.notes:
                record.notes.append("Adafruit nRF52 serial DFU bootloader detected")
            return
        if (
            record.device.upper().startswith("COM")
            and record.vid == ADAFRUIT_USB_VID
            and "usb serial" in haystack
            and not record.is_openpuck
        ):
            record.is_bootloader = True
            record.is_supported = True
            if "Possible Adafruit nRF52 serial DFU bootloader" not in record.notes:
                record.notes.append("Possible Adafruit nRF52 serial DFU bootloader")
            return
        if "bootloader" in haystack and "nrf52" in haystack:
            record.is_bootloader = True
            record.is_supported = True
            if "nRF52 bootloader interface detected" not in record.notes:
                record.notes.append("nRF52 bootloader interface detected")

    def _parse_int(self, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value, 16 if value.lower().startswith("0x") else 10)
            except ValueError:
                return None
        return None
