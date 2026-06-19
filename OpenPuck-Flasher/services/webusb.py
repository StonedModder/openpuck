from __future__ import annotations

from dataclasses import dataclass

try:
    import usb.core
    import usb.util
except Exception:  # pragma: no cover
    usb = None


OPENPUCK_USB_IDS = {
    (0x28DE, 0x1142): "Steam",
    (0x045E, 0x028E): "Xbox",
    (0x0F0D, 0x0092): "Hori Pad",
    (0x054C, 0x0CE6): "DualSense",
}


@dataclass(slots=True)
class WebUsbStatus:
    mode_name: str | None
    git_hash: str | None
    dirty: bool = False


class WebUsbProbe:
    def list_known_modes(self) -> dict[tuple[int, int], str]:
        return OPENPUCK_USB_IDS.copy()

    def read_openpuck_status(self) -> WebUsbStatus | None:
        if usb is None:
            return None
        try:
            dev = usb.core.find(idVendor=0x28DE, idProduct=0x1142)
        except Exception:
            return None
        if dev is None:
            return None
        intf = None
        try:
            cfg = dev.get_active_configuration()
            ep_out = None
            ep_in = None
            for interface in cfg:
                if interface.bInterfaceClass != 0xFF:
                    continue
                endpoints = list(interface.endpoints())
                for ep in endpoints:
                    direction = usb.util.endpoint_direction(ep.bEndpointAddress)
                    if direction == usb.util.ENDPOINT_OUT:
                        ep_out = ep
                    if direction == usb.util.ENDPOINT_IN:
                        ep_in = ep
                if ep_in and ep_out:
                    intf = interface
                    break
            if not (intf and ep_in and ep_out):
                return None
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
            ep_out.write(bytes([0x01]), timeout=1000)
            packet = bytes(ep_in.read(64, timeout=1000))
            if len(packet) < 2 or packet[0] != 0xA5:
                return None
            payload = packet[2 : 2 + packet[1]]
            dirty = len(payload) > 38 and payload[38] == 1
            git = ""
            for idx in range(39, min(51, len(payload))):
                if payload[idx] == 0:
                    break
                git += chr(payload[idx])
            return WebUsbStatus(
                mode_name="Steam",
                git_hash=git or None,
                dirty=dirty,
            )
        except Exception:
            return None
        finally:
            try:
                if intf:
                    usb.util.release_interface(dev, intf.bInterfaceNumber)
            except Exception:
                pass

    def reboot_to_bootloader(self, *, serial_only: bool) -> None:
        if usb is None:
            raise RuntimeError("WebUSB support is unavailable in this runtime.")
        try:
            dev = usb.core.find(idVendor=0x28DE, idProduct=0x1142)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"WebUSB lookup failed: {exc}") from exc
        if dev is None:
            raise RuntimeError("OpenPuck WebUSB device not found.")
        intf = None
        try:
            cfg = dev.get_active_configuration()
            ep_out = None
            for interface in cfg:
                if interface.bInterfaceClass != 0xFF:
                    continue
                for ep in interface.endpoints():
                    direction = usb.util.endpoint_direction(ep.bEndpointAddress)
                    if direction == usb.util.ENDPOINT_OUT:
                        ep_out = ep
                        intf = interface
                        break
                if ep_out:
                    break
            if not (intf and ep_out):
                raise RuntimeError("OpenPuck WebUSB control interface not found.")
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
            ep_out.write(bytes([0x09, 0x02 if serial_only else 0x01]), timeout=1000)
        finally:
            try:
                if intf:
                    usb.util.release_interface(dev, intf.bInterfaceNumber)
            except Exception:
                pass
