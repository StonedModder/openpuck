from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass

try:
    import pywinusb.hid as winhid
except Exception:  # pragma: no cover
    winhid = None


PUCK_PID = 0x1304
CONTROLLER_PIDS = {0x1301, 0x1302, 0x1303, 0x1205}
PAIRING_STATE_NAMES = {
    0: "idle",
    1: "searching",
    2: "controller seen",
    3: "bond captured",
    4: "connected",
    5: "timeout",
    6: "error",
}


@dataclass(slots=True)
class PuckNode:
    serial: str
    product_id: int
    product_name: str
    slot_paths: list[str]

    @property
    def label(self) -> str:
        return f"{self.serial} ({len(self.slot_paths)} HID slot path(s))"


@dataclass(slots=True)
class ControllerNode:
    serial: str
    product_id: int
    product_name: str
    path: str

    @property
    def label(self) -> str:
        return f"{self.serial} ({self.product_name})"


@dataclass(slots=True)
class PairingSlotInfo:
    index: int
    used: bool
    controller_serial: str
    proteus_uuid: str
    ibex_uuid: str
    path: str


@dataclass(slots=True)
class PairingStatus:
    enabled: bool
    state_code: int
    state_name: str
    slot: int
    channel: int


class PairingService:
    def available(self) -> bool:
        return os.name == "nt" and winhid is not None

    def list_pucks(self) -> list[PuckNode]:
        self._require_available()
        grouped: dict[str, list] = {}
        for dev in winhid.HidDeviceFilter(vendor_id=0x28DE, product_id=PUCK_PID).get_devices():
            serial = (dev.serial_number or "").strip()
            if not serial:
                continue
            grouped.setdefault(serial, []).append(dev)
        nodes: list[PuckNode] = []
        for serial, devices in grouped.items():
            ordered = sorted(devices, key=lambda item: self._slot_sort_key(item.device_path))
            nodes.append(
                PuckNode(
                    serial=serial,
                    product_id=ordered[0].product_id,
                    product_name=ordered[0].product_name or "Steam Controller Puck",
                    slot_paths=[dev.device_path for dev in ordered],
                )
            )
        return sorted(nodes, key=lambda item: item.serial)

    def list_controllers(self) -> list[ControllerNode]:
        self._require_available()
        nodes: list[ControllerNode] = []
        for dev in winhid.HidDeviceFilter(vendor_id=0x28DE).get_devices():
            if dev.product_id not in CONTROLLER_PIDS:
                continue
            serial = (dev.serial_number or "").strip()
            if not serial:
                continue
            nodes.append(
                ControllerNode(
                    serial=serial,
                    product_id=dev.product_id,
                    product_name=dev.product_name or "Steam Controller",
                    path=dev.device_path,
                )
            )
        dedup: dict[str, ControllerNode] = {}
        for node in nodes:
            dedup.setdefault(node.serial, node)
        return sorted(dedup.values(), key=lambda item: item.serial)

    def read_slots(self, puck_serial: str) -> list[PairingSlotInfo]:
        puck = self._find_puck(puck_serial)
        slots: list[PairingSlotInfo] = []
        for index, path in enumerate(puck.slot_paths):
            reply = self._send_command(path, 0xA3)
            payload = reply[3 : 3 + 24]
            used = any(payload[:8])
            ctrl_serial = bytes(payload[8:24]).split(b"\x00")[0].decode("ascii", errors="ignore")
            puuid = self._u32le_hex(payload[0:4]) if used else ""
            iuuid = self._u32le_hex(payload[4:8]) if used else ""
            slots.append(
                PairingSlotInfo(
                    index=index,
                    used=used,
                    controller_serial=ctrl_serial,
                    proteus_uuid=puuid,
                    ibex_uuid=iuuid,
                    path=path,
                )
            )
        return slots

    def read_status(self, puck_serial: str, slot: int = 0) -> PairingStatus:
        path = self._slot_path(self._find_puck(puck_serial), slot)
        reply = self._send_command(path, 0xAD)
        enabled = len(reply) > 3 and reply[3] != 0
        state_code = reply[4] if len(reply) > 4 else 0
        pairing_slot = reply[5] if len(reply) > 5 else slot
        channel = reply[6] if len(reply) > 6 else 2
        return PairingStatus(
            enabled=enabled,
            state_code=state_code,
            state_name=PAIRING_STATE_NAMES.get(state_code, f"unknown({state_code})"),
            slot=pairing_slot,
            channel=channel,
        )

    def read_connection_state(self, puck_serial: str, slot: int) -> int:
        path = self._slot_path(self._find_puck(puck_serial), slot)
        reply = self._send_command(path, 0xB4)
        return reply[3] if len(reply) > 3 else 0x01

    def start_rf_pairing(self, puck_serial: str, slot: int, channel: int) -> PairingStatus:
        if slot not in (0, 1):
            raise RuntimeError(
                "RF pairing currently supports puck slot 0 or 1 only. The controller-side tooling in this repo exposes two wireless bond stores, so slot 2 is not a proven pairing target."
            )
        path = self._slot_path(self._find_puck(puck_serial), slot)
        self._send_command(path, 0xAD, [0x01, 0x02])
        return self.read_status(puck_serial, slot)

    def stop_rf_pairing(self, puck_serial: str, slot: int) -> PairingStatus:
        path = self._slot_path(self._find_puck(puck_serial), slot)
        self._send_command(path, 0xAD, [0x00])
        return self.read_status(puck_serial, slot)

    def clear_slot(self, puck_serial: str, slot: int) -> None:
        path = self._slot_path(self._find_puck(puck_serial), slot)
        self._send_command(path, 0xA2, [0x00] * 24)

    def reboot_to_bootloader(self, puck_serial: str, *, serial_only: bool) -> None:
        puck = self._find_puck(puck_serial)
        path = self._slot_path(puck, 0)
        mode = 2 if serial_only else 1
        self._send_command(path, 0xF0, [mode], read_response=False)

    def host_assisted_pair(self, puck_serial: str, puck_slot: int, controller_serial: str) -> PairingSlotInfo:
        if puck_slot != 0:
            raise RuntimeError("Host-assisted pairing currently supports puck slot 0 only. The reference pairtui flow writes the controller's primary `esb/bond` store, and slot 0 is the only proven persistent path.")
        puck = self._find_puck(puck_serial)
        controller = self._find_controller(controller_serial)
        uuids = list(secrets.token_bytes(8))
        record = uuids + self._serial16(controller.serial)
        puck_path = self._slot_path(puck, puck_slot)
        self._send_command(puck_path, 0xAD, [0x01, 0x00])
        self._send_command(puck_path, 0xA2, record)
        self._send_command(puck_path, 0xAD, [0x00])

        key = "esb/bond"
        key_bytes = list(key.encode("ascii") + b"\x00")
        ctrl_payload = key_bytes + record[:8] + self._serial16(puck.serial)
        self._send_command(controller.path, 0xEE, ctrl_payload)
        self._send_command(controller.path, 0xEF, key_bytes)
        self._send_command(controller.path, 0x95, [0x52, 0xAF, 0x27, 0xA4])

        for slot_info in self.read_slots(puck_serial):
            if slot_info.index == puck_slot:
                return slot_info
        raise RuntimeError("Bond write completed but the puck slot could not be re-read.")

    def _send_command(
        self,
        path: str,
        cmd: int,
        payload: list[int] | None = None,
        report_id: int = 2,
        *,
        read_response: bool = True,
    ) -> list[int]:
        data = [report_id, cmd, len(payload or [])] + list(payload or [])
        if len(data) > 64:
            raise RuntimeError("HID feature payload exceeds 64 bytes.")
        data += [0] * (64 - len(data))
        device = self._open_path(path)
        try:
            report = next(rep for rep in device.find_feature_reports() if rep.report_id == report_id)
            report.send(data)
            if not read_response:
                return []
            return list(report.get())
        finally:
            device.close()

    def _open_path(self, path: str):
        for dev in winhid.HidDeviceFilter(vendor_id=0x28DE).get_devices():
            if dev.device_path == path:
                dev.open(shared=False)
                return dev
        raise RuntimeError(f"HID path is no longer present: {path}")

    def _find_puck(self, serial: str) -> PuckNode:
        for puck in self.list_pucks():
            if puck.serial == serial:
                return puck
        raise RuntimeError(f"OpenPuck HID interfaces not found for serial {serial}.")

    def _find_controller(self, serial: str) -> ControllerNode:
        for controller in self.list_controllers():
            if controller.serial == serial:
                return controller
        raise RuntimeError(f"Docked Steam controller not found for serial {serial}.")

    def _slot_path(self, puck: PuckNode, slot: int) -> str:
        if slot < 0 or slot >= len(puck.slot_paths):
            raise RuntimeError(f"Requested puck slot {slot} is not exposed on this host. Found {len(puck.slot_paths)} HID slot path(s).")
        return puck.slot_paths[slot]

    def _require_available(self) -> None:
        if not self.available():
            raise RuntimeError("Windows HID pairing support is unavailable. Install the Windows build with pywinusb bundled.")

    def _slot_sort_key(self, path: str) -> tuple[int, str]:
        match = re.search(r"mi_(\d+)", path, re.IGNORECASE)
        return (int(match.group(1)) if match else 999, path)

    def _u32le_hex(self, data: list[int]) -> str:
        if len(data) < 4:
            return ""
        return f"0x{int.from_bytes(bytes(data[:4]), 'little'):08X}"

    def _serial16(self, serial: str) -> list[int]:
        raw = serial.encode("ascii", errors="ignore")[:16]
        return list(raw + (b"\x00" * (16 - len(raw))))
