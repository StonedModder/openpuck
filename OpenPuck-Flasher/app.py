from __future__ import annotations

import ctypes
import queue
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import StringVar

import customtkinter as ctk
import serial

from . import __version__
from .config import AppConfig, ConfigStore
from .models import BuildArtifacts, PortRecord
from .services import DeviceService, OpenPuckService, PairingService, UpdateChecker
from .services.http_panel import LocalPanelServer


CYAN = "#5eead4"
LIME = "#bef264"
AMBER = "#fbbf24"
ROSE = "#fb7185"
SURFACE = "#111827"
PANEL = "#0b1220"


def apply_dpi_awareness() -> None:
    if hasattr(ctypes, "windll"):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


class ToolTip:
    def __init__(self, widget: ctk.CTkBaseClass, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: ctk.CTkToplevel | None = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None) -> None:
        if self.tip:
            return
        self.tip = ctk.CTkToplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip.geometry(f"+{x}+{y}")
        label = ctk.CTkLabel(
            self.tip,
            text=self.text,
            fg_color="#172033",
            corner_radius=8,
            padx=10,
            pady=8,
            justify="left",
            text_color="#d1fae5",
        )
        label.pack()

    def hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
        self.tip = None


class SerialMonitorWindow(ctk.CTkToplevel):
    def __init__(self, master: "OpenPuckFlasherApp", port: str, baud: int) -> None:
        super().__init__(master)
        self.master_app = master
        self.port = port
        self.baud = baud
        self.title(f"Serial Monitor - {port}")
        self.geometry("860x460")
        self.configure(fg_color=PANEL)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._stop = threading.Event()
        self._serial: serial.Serial | None = None

        header = ctk.CTkFrame(self, fg_color="#122033", corner_radius=16)
        header.pack(fill="x", padx=14, pady=(14, 10))
        ctk.CTkLabel(
            header,
            text=f"{port} @ {baud}",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=CYAN,
        ).pack(side="left", padx=12, pady=10)
        ctk.CTkButton(
            header,
            text="Send ERASE-ALL",
            fg_color="#3a1420",
            hover_color="#5f1d33",
            command=lambda: self.send_line("ERASE-ALL"),
        ).pack(side="right", padx=12, pady=10)

        self.output = ctk.CTkTextbox(self, fg_color="#030712", text_color="#d1fae5", wrap="word")
        self.output.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=14, pady=(0, 14))
        self.input_var = StringVar()
        entry = ctk.CTkEntry(footer, textvariable=self.input_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.bind("<Return>", lambda _e: self.send_line(self.input_var.get()))
        ctk.CTkButton(footer, text="Send", fg_color=CYAN, text_color="#04111d", command=lambda: self.send_line(self.input_var.get())).pack(side="left")

        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=0.25)
            self._append(f"Connected to {self.port}")
            while not self._stop.is_set():
                data = self._serial.readline()
                if data:
                    self._append(data.decode("utf-8", errors="replace").rstrip())
        except Exception as exc:
            self._append(f"[serial error] {exc}")

    def send_line(self, line: str) -> None:
        if not line:
            return
        self.input_var.set("")
        self._append(f"> {line}")
        if not self._serial:
            return
        try:
            self._serial.write(line.encode("utf-8") + b"\r\n")
            self._serial.flush()
        except Exception as exc:
            self._append(f"[write error] {exc}")

    def _append(self, line: str) -> None:
        self.after(0, lambda: (self.output.insert("end", line + "\n"), self.output.see("end")))

    def _close(self) -> None:
        self._stop.set()
        try:
            if self._serial:
                self._serial.close()
        finally:
            self.destroy()


class OpenPuckFlasherApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        apply_dpi_awareness()
        self.store = ConfigStore()
        self.config_data = self.store.load()
        ctk.set_appearance_mode(self.config_data.theme_mode)
        ctk.set_default_color_theme("dark-blue")
        self._apply_scaling()

        self.title("OpenPuck Flasher")
        self.geometry("1360x900")
        self.minsize(1160, 760)
        self.configure(fg_color=SURFACE)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.devices: list[PortRecord] = []
        self.device_map: dict[str, PortRecord] = {}
        self.selected_device = StringVar()
        self.uf2_target = StringVar()
        self.package_path = StringVar()
        self.update_status = StringVar(value="Idle")
        self.pairing_status = StringVar(value="Pairing idle")
        self.pairing_puck = StringVar(value="No puck detected")
        self.pairing_controller = StringVar(value="No docked controller")
        self.pairing_slot = StringVar(value="0")
        self.pairing_channel = StringVar(value="0x02")
        self.current_build: BuildArtifacts | None = None
        self.selected_device_id: str | None = None
        self.preferred_device_id: str | None = None
        self.expect_bootloader_after_refresh = False
        self.active_task_count = 0
        self.pairing_active = False
        self.pairing_puck_map: dict[str, object] = {}
        self.pairing_controller_map: dict[str, object] = {}

        self.service = OpenPuckService(self.config_data)
        self.detector = DeviceService(self.config_data)
        self.pairing_service = PairingService()
        self.updates = UpdateChecker()
        self.panel_server = LocalPanelServer()

        self._build_shell()
        self._restore_artifact_state()
        self.after(100, self._drain_logs)
        self.after(250, self.refresh_devices_passive)
        if self.config_data.check_for_updates:
            self.after(600, self.check_updates)
        if self.pairing_service.available():
            self.after(900, self.refresh_pairing_inventory)
        if self.config_data.auto_refresh_devices:
            self.after(10000, self._device_poll_tick)

    def _apply_scaling(self) -> None:
        scaling = max(0.8, min(2.2, self.config_data.ui_scaling))
        ctk.set_widget_scaling(scaling)
        ctk.set_window_scaling(scaling)

    def _build_shell(self) -> None:
        hero = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=22, border_width=1, border_color="#1f2937")
        hero.pack(fill="x", padx=18, pady=(18, 12))

        left = ctk.CTkFrame(hero, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=18, pady=16)
        ctk.CTkLabel(
            left,
            text="OpenPuck Flasher",
            font=ctk.CTkFont(size=30, weight="bold"),
            text_color="#ecfeff",
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="One-click build, flash, DFU, reset, WebUSB launch, and board diagnostics for OpenPuck.",
            text_color="#9ca3af",
        ).pack(anchor="w", pady=(6, 0))
        self.status_chip = ctk.CTkLabel(
            hero,
            text="Waiting for device scan",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#122033",
            corner_radius=999,
            padx=18,
            pady=8,
            text_color=CYAN,
        )
        self.status_chip.pack(side="right", padx=18, pady=18)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 18))

        left_host = ctk.CTkFrame(body, fg_color="transparent")
        left_host.pack(side="left", fill="both", expand=True, padx=(0, 10))

        left_col = ctk.CTkScrollableFrame(left_host, fg_color="transparent")
        left_col.pack(fill="both", expand=True)

        right_col = ctk.CTkFrame(body, fg_color="transparent", width=430)
        right_col.pack(side="right", fill="both", padx=(10, 0))
        right_col.pack_propagate(False)

        self._build_device_card(left_col)
        self._build_tabs(left_col)
        self._build_log_card(right_col)

    def _build_device_card(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=20, border_width=1, border_color="#1f2937")
        card.pack(fill="x", pady=(0, 12))
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 10))
        ctk.CTkLabel(header, text="Detected Boards", font=ctk.CTkFont(size=20, weight="bold"), text_color=LIME).pack(side="left")
        ctk.CTkButton(header, text="Deep Refresh", width=110, fg_color=CYAN, text_color="#04111d", command=self.refresh_devices).pack(side="right")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        self.device_combo = ctk.CTkComboBox(row, variable=self.selected_device, values=["Scanning..."])
        self.device_combo.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(row, text="Serial Monitor", width=140, fg_color="#153047", command=self.open_serial_monitor).pack(side="left")

        status_row = ctk.CTkFrame(card, fg_color="transparent")
        status_row.pack(fill="x", padx=16, pady=(0, 8))
        self.device_mode_label = ctk.CTkLabel(status_row, text="Mode: unknown", text_color="#93c5fd")
        self.device_mode_label.pack(side="left")
        self.device_build_label = ctk.CTkLabel(status_row, text="Build: unknown", text_color="#c4b5fd")
        self.device_build_label.pack(side="right")
        self.device_health_label = ctk.CTkLabel(
            card,
            text="Health: waiting for detection",
            text_color="#fbbf24",
            justify="left",
        )
        self.device_health_label.pack(anchor="w", padx=16, pady=(0, 10))

        self.device_summary = ctk.CTkTextbox(card, height=120, fg_color="#030712", text_color="#d1fae5")
        self.device_summary.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkLabel(
            card,
            text="Auto-refresh is passive. Use Deep Refresh when you want an arduino-cli board probe.",
            text_color="#94a3b8",
        ).pack(anchor="w", padx=16, pady=(0, 14))

    def _build_tabs(self, parent) -> None:
        tabs = ctk.CTkTabview(parent, fg_color=PANEL, segmented_button_fg_color="#111827", segmented_button_selected_color="#164e63")
        tabs.pack(fill="both", expand=True)
        tabs.add("Build & Flash")
        tabs.add("Tools")
        tabs.add("Settings")
        tabs.add("Pairing")

        self._build_build_tab(tabs.tab("Build & Flash"))
        self._build_tools_tab(tabs.tab("Tools"))
        self._build_settings_tab(tabs.tab("Settings"))
        self._build_pairing_tab(tabs.tab("Pairing"))

    def _build_build_tab(self, parent) -> None:
        parent.grid_columnconfigure((0, 1), weight=1)

        build_card = self._section(parent, "Firmware Build", "Wraps the BUILD_AND_DEPLOY compile flow with version stamping and artifact discovery.")
        build_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
        ToolTip(build_card, "Runs gen_version.sh, then arduino-cli compile with -DNRF52840_XXAA and -DCFG_TUD_HID=4.")

        ctk.CTkButton(build_card, text="Build Firmware", fg_color=CYAN, text_color="#04111d", command=lambda: self._run_task("Build firmware", lambda: self._build(False))).pack(fill="x", padx=14, pady=(8, 8))
        ctk.CTkButton(build_card, text="Build Factory Reset Firmware", fg_color="#334155", command=lambda: self._run_task("Build factory reset", lambda: self._build(True))).pack(fill="x", padx=14, pady=(0, 12))
        self.artifact_label = ctk.CTkLabel(
            build_card,
            text="Artifacts: none yet",
            text_color="#9ca3af",
            justify="left",
            wraplength=460,
        )
        self.artifact_label.pack(anchor="w", padx=14, pady=(0, 14))

        flash_card = self._section(parent, "Flash Paths", "Serial DFU is the recommended update path for clone boards that do not expose a reset button.")
        flash_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))
        ctk.CTkButton(
            flash_card,
            text="Build + Update via Serial DFU",
            fg_color="#1d4ed8",
            hover_color="#1e40af",
            command=lambda: self._run_task("Build and update DFU", self._build_and_flash_dfu),
        ).pack(fill="x", padx=14, pady=(8, 8))
        ctk.CTkButton(
            flash_card,
            text="Update via Serial DFU",
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            command=lambda: self._run_task("Flash DFU", self._flash_dfu),
        ).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(
            flash_card,
            text="Reboot Puck To Serial DFU",
            fg_color="#0f766e",
            hover_color="#115e59",
            command=lambda: self._run_task("Reboot to serial DFU", lambda: self._reboot_puck_to_bootloader(serial_only=True)),
        ).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(
            flash_card,
            text="Reboot Puck To UF2 Bootloader",
            fg_color="#155e75",
            hover_color="#164e63",
            command=lambda: self._run_task("Reboot to UF2", lambda: self._reboot_puck_to_bootloader(serial_only=False)),
        ).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(
            flash_card,
            text="Recommended for OpenPuck updates on clones without a reset button. On updated firmware, the reboot buttons can jump directly into DFU/UF2 bootloader mode without physical reset access.",
            text_color="#bfdbfe",
            wraplength=450,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 10))
        ctk.CTkButton(flash_card, text="Flash via arduino-cli", fg_color=LIME, text_color="#122013", command=lambda: self._run_task("Flash serial", self._flash_serial)).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(flash_card, text="UF2 bootloader target path", text_color="#d1d5db").pack(anchor="w", padx=14)
        ctk.CTkEntry(flash_card, textvariable=self.uf2_target).pack(fill="x", padx=14, pady=(6, 8))
        ctk.CTkButton(flash_card, text="Copy UF2 to Boot Drive", fg_color=AMBER, text_color="#201408", command=lambda: self._run_task("Copy UF2", self._copy_uf2)).pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(flash_card, text="Manual DFU package override (.zip)", text_color="#d1d5db").pack(anchor="w", padx=14)
        ctk.CTkEntry(flash_card, textvariable=self.package_path).pack(fill="x", padx=14, pady=(6, 14))

        extras = self._section(parent, "Workflow Helpers", "Bootstrap core dependencies and compare your local repo against upstream.")
        extras.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 0))
        button_row = ctk.CTkFrame(extras, fg_color="transparent")
        button_row.pack(fill="x", padx=14, pady=(10, 10))
        ctk.CTkButton(button_row, text="Install/Repair Tooling", fg_color="#155e75", command=lambda: self._run_task("Install tooling", lambda: self.service.install_core(self._log))).pack(side="left", padx=(0, 10))
        ctk.CTkButton(button_row, text="Check Upstream Updates", fg_color="#3f3f46", command=self.check_updates).pack(side="left")
        ctk.CTkLabel(button_row, textvariable=self.update_status, text_color="#93c5fd").pack(side="right")
        recovery = (
            "No COM port after flashing is often normal in puck mode.\n"
            "OpenPuck can drop CDC serial and stay visible only as a Valve/OpenPuck HID puck.\n\n"
            "Recovery/update path:\n"
            "1. Use Pairing tab -> Refresh Pairing HID.\n"
            "2. If a puck is found there, the firmware is still alive.\n"
            "3. Use Serial DFU or host-assisted pairing from the GUI instead of waiting for a COM port.\n"
            "4. Only expect a COM port when booted into a debug CDC build or bootloader/DFU path."
        )
        ctk.CTkLabel(
            extras,
            text=recovery,
            text_color="#cbd5e1",
            wraplength=920,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 14))

    def _build_tools_tab(self, parent) -> None:
        parent.grid_columnconfigure((0, 1), weight=1)

        reset_card = self._section(parent, "Reset & Recovery", "One-shot reset build plus direct serial ERASE-ALL.")
        reset_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
        ctk.CTkButton(reset_card, text="Send ERASE-ALL", fg_color="#7f1d1d", hover_color="#991b1b", command=lambda: self._run_task("Factory erase", self._erase_all)).pack(fill="x", padx=14, pady=(8, 8))
        ctk.CTkLabel(reset_card, text="Use the factory reset build if the serial console is unavailable in the current USB mode.", text_color="#9ca3af", wraplength=440, justify="left").pack(anchor="w", padx=14, pady=(0, 14))

        panel_card = self._section(parent, "WebUSB Panel", "Launch the repo's docs/ panel on localhost so Chrome or Edge gets a secure context.")
        panel_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))
        ctk.CTkButton(panel_card, text="Launch WebUSB Panel", fg_color="#0f766e", command=self.launch_panel).pack(fill="x", padx=14, pady=(8, 8))
        ctk.CTkButton(panel_card, text="Stop Local Panel Server", fg_color="#374151", command=self.stop_panel).pack(fill="x", padx=14, pady=(0, 14))

        docs_card = self._section(parent, "Documentation Shortcuts", "Reference the original workflow before or during troubleshooting.")
        docs_card.grid(row=1, column=0, columnspan=2, sticky="nsew")
        ctk.CTkButton(docs_card, text="Open BUILD_AND_DEPLOY.md", fg_color="#1f2937", command=lambda: webbrowser.open(Path(self.config_data.repo_root, self.config_data.tooltip_reference).resolve().as_uri())).pack(fill="x", padx=14, pady=(10, 8))
        ctk.CTkButton(docs_card, text="Open Testing Guide", fg_color="#1f2937", command=lambda: webbrowser.open(Path(self.config_data.repo_root, "docs", "TESTING_GUIDE.md").resolve().as_uri())).pack(fill="x", padx=14, pady=(0, 14))

    def _build_settings_tab(self, parent) -> None:
        entries = [
            ("Repository Root", "repo_root"),
            ("Build Root", "build_root"),
            ("arduino-cli Path", "arduino_cli_path"),
            ("Python Path", "python_path"),
            ("adafruit-nrfutil Path", "adafruit_nrfutil_path"),
            ("Board FQBN", "board_fqbn"),
            ("Update Endpoint", "update_source"),
        ]
        self.settings_vars: dict[str, StringVar] = {}
        for index, (label, field) in enumerate(entries):
            ctk.CTkLabel(parent, text=label, text_color="#d1d5db").grid(row=index, column=0, sticky="w", padx=16, pady=(10 if index == 0 else 6, 0))
            value = StringVar(value=str(getattr(self.config_data, field)))
            self.settings_vars[field] = value
            ctk.CTkEntry(parent, textvariable=value, width=720).grid(row=index, column=1, sticky="ew", padx=(0, 16), pady=(10 if index == 0 else 6, 0))

        self.scaling_var = StringVar(value=str(self.config_data.ui_scaling))
        ctk.CTkLabel(parent, text="UI Scaling", text_color="#d1d5db").grid(row=len(entries), column=0, sticky="w", padx=16, pady=6)
        ctk.CTkEntry(parent, textvariable=self.scaling_var).grid(row=len(entries), column=1, sticky="w", padx=(0, 16), pady=6)

        ctk.CTkButton(parent, text="Save Settings", fg_color=CYAN, text_color="#04111d", command=self.save_settings).grid(row=len(entries) + 1, column=0, columnspan=2, sticky="ew", padx=16, pady=16)
        ctk.CTkLabel(parent, text="Restart the app after changing repo paths or scaling for the cleanest result on mixed-DPI systems.", text_color="#9ca3af", wraplength=900, justify="left").grid(row=len(entries) + 2, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 12))

    def _build_pairing_tab(self, parent) -> None:
        if not self.pairing_service.available():
            box = ctk.CTkTextbox(parent, fg_color="#030712", text_color="#d1fae5")
            box.pack(fill="both", expand=True, padx=16, pady=16)
            box.insert(
                "1.0",
                "Windows HID pairing support is unavailable in this runtime.\n\n"
                "Rebuild the Windows EXE so `pywinusb` is bundled, then use this tab for RF pairing status, host-assisted bond provisioning, and slot verification.",
            )
            return

        parent.grid_columnconfigure((0, 1), weight=1)

        inv = self._section(parent, "Pairing Inventory", "Reads the puck HID slot interfaces and any docked Steam controllers.")
        inv.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        row = ctk.CTkFrame(inv, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(10, 8))
        self.pairing_puck_combo = ctk.CTkComboBox(row, variable=self.pairing_puck, values=["No puck detected"])
        self.pairing_puck_combo.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.pairing_controller_combo = ctk.CTkComboBox(row, variable=self.pairing_controller, values=["No docked controller"])
        self.pairing_controller_combo.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(row, text="Refresh Pairing HID", fg_color=CYAN, text_color="#04111d", command=self.refresh_pairing_inventory).pack(side="left")
        self.pairing_slots_box = ctk.CTkTextbox(inv, height=120, fg_color="#030712", text_color="#d1fae5")
        self.pairing_slots_box.pack(fill="x", padx=14, pady=(0, 14))

        target = self._section(parent, "Pairing Target", "Choose the puck slot once here. Both RF pairing and host-assisted pairing use this target.")
        target.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        target_row = ctk.CTkFrame(target, fg_color="transparent")
        target_row.pack(fill="x", padx=14, pady=(10, 8))
        ctk.CTkLabel(target_row, text="Puck Slot", text_color="#d1d5db").pack(side="left")
        self.pairing_slot_combo = ctk.CTkComboBox(
            target_row,
            variable=self.pairing_slot,
            values=["0", "1"],
            width=90,
        )
        self.pairing_slot_combo.pack(side="left", padx=(8, 16))
        ctk.CTkLabel(target_row, text="Discovery Channel", text_color="#d1d5db").pack(side="left")
        chan_entry = ctk.CTkEntry(target_row, textvariable=self.pairing_channel, width=90)
        chan_entry.pack(side="left", padx=(8, 0))
        chan_entry.configure(state="disabled")
        ctk.CTkLabel(
            target,
            text="RF pairing uses the real shared discovery channel (`ibex` / channel 2). Use puck slot 0 or 1 only: the controller-side tooling in this repo exposes two wireless bond stores. Host-assisted pairing remains slot 0 only.",
            text_color="#fbbf24",
            wraplength=900,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 14))

        rf = self._section(parent, "RF Pairing", "Starts real puck pairing mode on the selected target slot and polls firmware state from `0xAD`.")
        rf.grid(row=2, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
        ctk.CTkButton(rf, text="Start RF Pairing", fg_color="#1d4ed8", command=lambda: self._run_task("Start RF pairing", self._start_rf_pairing)).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(rf, text="Stop RF Pairing", fg_color="#334155", command=lambda: self._run_task("Stop RF pairing", self._stop_rf_pairing)).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(rf, text="Poll Pairing Status", fg_color="#155e75", command=lambda: self._run_task("Poll pairing status", self._poll_pairing_status)).pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(rf, textvariable=self.pairing_status, text_color="#93c5fd", wraplength=420, justify="left").pack(anchor="w", padx=14, pady=(0, 14))

        host = self._section(parent, "Host-Assisted Pairing", "Pairs a docked controller by provisioning the initial bond over USB, similar to pairtui.")
        host.grid(row=2, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))
        ctk.CTkLabel(host, text="Controller must be USB-docked to the PC directly. This path writes puck slot 0 and the matching controller bond, then reboots the controller back to wireless.", text_color="#bfdbfe", wraplength=430, justify="left").pack(anchor="w", padx=14, pady=(10, 8))
        ctk.CTkLabel(host, text="Supported host-assisted slot: 0 only. The repo tooling exposes controller-side `esb/bond` and `esb/bond_2`, so use RF pairing only on puck slot 0 or 1.", text_color="#fbbf24", wraplength=430, justify="left").pack(anchor="w", padx=14, pady=(0, 10))
        ctk.CTkButton(host, text="Provision Selected Slot + Reboot Controller", fg_color=LIME, text_color="#122013", command=lambda: self._run_task("Host-assisted pairing", self._host_assisted_pair)).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(host, text="Clear Selected Puck Slot", fg_color="#7f1d1d", hover_color="#991b1b", command=lambda: self._run_task("Clear puck slot", self._clear_pairing_slot)).pack(fill="x", padx=14, pady=(0, 12))

        debug = self._section(parent, "Pairing Debug", "Firmware pairing state, slot content, and controller-side provisioning activity.")
        debug.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self.pairing_debug_box = ctk.CTkTextbox(debug, height=220, fg_color="#020617", text_color="#d1fae5", font=ctk.CTkFont(family="Consolas", size=13))
        self.pairing_debug_box.pack(fill="both", expand=True, padx=14, pady=(10, 14))

    def _build_log_card(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=20, border_width=1, border_color="#1f2937")
        card.pack(fill="both", expand=True)
        ctk.CTkLabel(card, text="Activity Log", font=ctk.CTkFont(size=20, weight="bold"), text_color=CYAN).pack(anchor="w", padx=16, pady=(16, 8))
        self.log_box = ctk.CTkTextbox(card, fg_color="#020617", text_color="#d1fae5", font=ctk.CTkFont(family="Consolas", size=13))
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def _section(self, parent, title: str, subtitle: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="#0f172a", corner_radius=18, border_width=1, border_color="#1e293b")
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=18, weight="bold"), text_color="#f8fafc").pack(anchor="w", padx=14, pady=(14, 2))
        ctk.CTkLabel(frame, text=subtitle, text_color="#94a3b8", wraplength=450, justify="left").pack(anchor="w", padx=14, pady=(0, 0))
        return frame

    def save_settings(self) -> None:
        for field, var in self.settings_vars.items():
            setattr(self.config_data, field, var.get())
        self.config_data.ui_scaling = float(self.scaling_var.get())
        self.store.save(self.config_data)
        self.service = OpenPuckService(self.config_data)
        self.detector = DeviceService(self.config_data)
        self._log("Settings saved.")

    def refresh_pairing_inventory(self) -> None:
        self._run_task("Refresh pairing HID", self._refresh_pairing_inventory_inner)

    def _refresh_pairing_inventory_inner(self) -> None:
        pucks = self.pairing_service.list_pucks()
        controllers = self.pairing_service.list_controllers()
        self.pairing_puck_map = {puck.label: puck for puck in pucks}
        self.pairing_controller_map = {controller.label: controller for controller in controllers}
        puck_values = list(self.pairing_puck_map.keys()) or ["No puck detected"]
        controller_values = list(self.pairing_controller_map.keys()) or ["No docked controller"]
        self.after(0, lambda: self.pairing_puck_combo.configure(values=puck_values))
        self.after(0, lambda: self.pairing_controller_combo.configure(values=controller_values))
        self.after(0, lambda: self.pairing_puck.set(puck_values[0]))
        self.after(0, lambda: self.pairing_controller.set(controller_values[0]))
        self.after(0, self._render_pairing_slots)
        self._pair_log(f"Found {len(pucks)} puck(s) and {len(controllers)} docked controller(s).")

    def _render_pairing_slots(self) -> None:
        if not hasattr(self, "pairing_slots_box"):
            return
        puck = self._selected_pairing_puck()
        if not puck:
            self.after(0, lambda: (self.pairing_slots_box.delete("1.0", "end"), self.pairing_slots_box.insert("end", "No puck HID interface found.")))
            return
        slots = self.pairing_service.read_slots(puck.serial)
        lines = [f"Puck {puck.serial}"]
        for slot in slots:
            state = "bonded" if slot.used else "empty"
            who = slot.controller_serial or "none"
            ids = f"{slot.proteus_uuid} / {slot.ibex_uuid}" if slot.used else "-"
            lines.append(f"slot {slot.index}: {state}  controller={who}  uuids={ids}")
        text = "\n".join(lines)
        self.after(0, lambda: (self.pairing_slots_box.delete("1.0", "end"), self.pairing_slots_box.insert("end", text)))

    def _selected_pairing_puck(self):
        return self.pairing_puck_map.get(self.pairing_puck.get())

    def _selected_controller(self):
        return self.pairing_controller_map.get(self.pairing_controller.get())

    def _parse_pairing_slot(self) -> int:
        return max(0, int(self.pairing_slot.get().strip() or "0"))

    def _parse_pairing_channel(self) -> int:
        raw = self.pairing_channel.get().strip() or "0x3C"
        return int(raw, 16 if raw.lower().startswith("0x") else 10)

    def _start_rf_pairing(self) -> None:
        puck = self._selected_pairing_puck()
        if not puck:
            raise RuntimeError("No OpenPuck HID device selected.")
        slot = self._parse_pairing_slot()
        channel = self._parse_pairing_channel()
        status = self.pairing_service.start_rf_pairing(puck.serial, slot, channel)
        self.pairing_active = True
        self.after(0, lambda: self.pairing_status.set(f"RF pairing active on slot {status.slot}, channel 0x{status.channel:02X}: {status.state_name}"))
        self._pair_log(f"RF pairing started on {puck.serial} slot {slot} channel 0x{channel:02X}. Put the controller into pairing mode now.")
        self.after(1500, self._pairing_poll_tick)

    def _stop_rf_pairing(self) -> None:
        puck = self._selected_pairing_puck()
        if not puck:
            raise RuntimeError("No OpenPuck HID device selected.")
        slot = self._parse_pairing_slot()
        status = self.pairing_service.stop_rf_pairing(puck.serial, slot)
        self.pairing_active = False
        self.after(0, lambda: self.pairing_status.set(f"RF pairing stopped: {status.state_name}"))
        self._pair_log(f"RF pairing stopped on {puck.serial} slot {slot}.")

    def _poll_pairing_status(self) -> None:
        puck = self._selected_pairing_puck()
        if not puck:
            raise RuntimeError("No OpenPuck HID device selected.")
        slot = self._parse_pairing_slot()
        status = self.pairing_service.read_status(puck.serial, slot)
        conn = self.pairing_service.read_connection_state(puck.serial, slot)
        slots = self.pairing_service.read_slots(puck.serial)
        bond = next((item for item in slots if item.index == slot), None)
        parts = [f"slot {status.slot}", f"channel 0x{status.channel:02X}", status.state_name]
        if conn == 0x02:
            parts.append("connected")
        if bond and bond.used:
            parts.append(f"bonded to {bond.controller_serial or 'controller'}")
        self.after(0, lambda: self.pairing_status.set(" | ".join(parts)))
        self._render_pairing_slots()
        self._pair_log(f"Pairing status: {' | '.join(parts)}")
        if status.state_code in (3, 4):
            self.pairing_active = False

    def _host_assisted_pair(self) -> None:
        puck = self._selected_pairing_puck()
        controller = self._selected_controller()
        if not puck:
            raise RuntimeError("No OpenPuck HID device selected.")
        if not controller:
            raise RuntimeError("No docked controller selected.")
        slot = self._parse_pairing_slot()
        if slot != 0:
            raise RuntimeError(
                "Host-assisted pairing only supports puck slot 0. Change the Pairing Target slot to 0, or use RF pairing for other slots."
            )
        result = self.pairing_service.host_assisted_pair(puck.serial, slot, controller.serial)
        self.after(0, lambda: self.pairing_status.set(f"Host-assisted pairing written to slot {slot}: {result.controller_serial or controller.serial}"))
        self._pair_log(f"Provisioned puck slot {slot} for docked controller {controller.serial}. Undock the controller and let it reconnect wirelessly.")
        self._render_pairing_slots()

    def _clear_pairing_slot(self) -> None:
        puck = self._selected_pairing_puck()
        if not puck:
            raise RuntimeError("No OpenPuck HID device selected.")
        slot = self._parse_pairing_slot()
        self.pairing_service.clear_slot(puck.serial, slot)
        self.after(0, lambda: self.pairing_status.set(f"Cleared puck slot {slot}"))
        self._pair_log(f"Cleared puck slot {slot}.")
        self._render_pairing_slots()

    def _pairing_poll_tick(self) -> None:
        if not self.pairing_active or self.active_task_count != 0:
            return
        threading.Thread(target=self._pairing_poll_wrapper, daemon=True).start()

    def _pairing_poll_wrapper(self) -> None:
        try:
            self._poll_pairing_status()
        except Exception as exc:
            self._pair_log(f"[poll error] {exc}")
            self.pairing_active = False
            return
        if self.pairing_active:
            self.after(1500, self._pairing_poll_tick)

    def _pair_log(self, line: str) -> None:
        self._log(f"[pairing] {line}")
        if hasattr(self, "pairing_debug_box"):
            self.after(
                0,
                lambda: (
                    self.pairing_debug_box.insert("end", f"{time.strftime('%H:%M:%S')}  {line}\n"),
                    self.pairing_debug_box.see("end"),
                ),
            )

    def refresh_devices(self) -> None:
        self._run_task("Refresh devices", lambda: self._refresh_devices_inner(include_cli_probe=True))

    def refresh_devices_passive(self) -> None:
        self._run_task("Refresh devices", lambda: self._refresh_devices_inner(include_cli_probe=False))

    def _refresh_devices_inner(self, *, include_cli_probe: bool) -> None:
        devices = self.detector.detect(include_cli_probe=include_cli_probe)
        self.devices = devices
        self.device_map = {device.display_name: device for device in devices}
        values = list(self.device_map.keys()) or ["No devices found"]
        self.after(0, lambda: self.device_combo.configure(values=values))
        current_device = self._current_selected_device_id()
        next_value = (
            self._display_name_for_device(current_device)
            or self._display_name_for_device(self.preferred_device_id)
            or self._preferred_display_name(devices)
            or values[0]
        )
        self.selected_device_id = self.device_map.get(next_value).device if next_value in self.device_map else None
        if self.selected_device_id:
            self.preferred_device_id = self.selected_device_id
        if any(device.is_bootloader for device in devices):
            self.expect_bootloader_after_refresh = False
        self.after(0, lambda: self.selected_device.set(next_value))
        self.after(0, self._render_device_panels)
        self._set_status(f"{len(devices)} device(s) scanned")

    def _render_device_panels(self) -> None:
        self._render_device_summary()
        self._render_selected_device_status()

    def _render_device_summary(self) -> None:
        self.device_summary.delete("1.0", "end")
        if not self.devices:
            self.device_summary.insert("end", "No boards detected.\nConnect an nRF52840 Pro Micro / Feather board or an already-flashed OpenPuck.")
            return
        lines = []
        for device in self.devices:
            lines.append(device.display_name)
            if device.board_name or device.fqbn:
                lines.append(f"  board: {device.board_name or 'unknown'}")
            if device.openpuck_mode:
                lines.append(f"  mode: {device.openpuck_mode}")
            if device.openpuck_build:
                lines.append(f"  build: {device.openpuck_build}")
            if device.vid is not None and device.pid is not None:
                lines.append(f"  usb: {device.vid:04X}:{device.pid:04X}")
            for note in device.notes:
                lines.append(f"  note: {note}")
            lines.append("")
        self.device_summary.insert("end", "\n".join(lines).rstrip())

    def _render_selected_device_status(self) -> None:
        record = self._record_for_device(self._current_selected_device_id())
        if not record:
            self.device_mode_label.configure(text="Mode: unknown")
            self.device_build_label.configure(text="Build: unknown")
            self.device_health_label.configure(text="Health: no device selected", text_color=AMBER)
            return
        mode = record.openpuck_mode or ("Supported nRF52840" if record.is_supported else "Unknown")
        build = record.openpuck_build or ("Not readable yet" if record.is_openpuck else "Unknown")
        self.device_mode_label.configure(text=f"Mode: {mode}")
        self.device_build_label.configure(text=f"Build: {build}")
        if record.is_bootloader:
            self.device_health_label.configure(
                text="Health: serial DFU bootloader detected. Use Update via Serial DFU on this port.",
                text_color=LIME,
            )
            return
        if record.is_openpuck and record.openpuck_mode == "Steam":
            self.device_health_label.configure(
                text="Health: pairing-ready, OpenPuck Steam USB identity is present",
                text_color=LIME,
            )
        elif record.is_openpuck:
            self.device_health_label.configure(
                text=f"Health: OpenPuck detected, but current USB mode is {record.openpuck_mode}. Pairing expects Steam mode.",
                text_color=AMBER,
            )
        elif record.is_supported:
            self.device_health_label.configure(
                text="Health: nRF52840 board detected, but it is not currently presenting as the OpenPuck Steam device",
                text_color=ROSE,
            )
        else:
            self.device_health_label.configure(
                text="Health: selected device is not recognized as OpenPuck or a supported nRF52840 board",
                text_color=ROSE,
            )

    def _device_poll_tick(self) -> None:
        if self.config_data.auto_refresh_devices:
            if self.active_task_count == 0:
                self.refresh_devices_passive()
            self.after(10000, self._device_poll_tick)

    def _current_port(self) -> str:
        device_id = self._current_selected_device_id()
        device = self._record_for_device(device_id)
        if not device:
            raise RuntimeError("No device selected.")
        return device.device

    def _build(self, factory_reset: bool) -> None:
        artifacts = self.service.build_firmware(factory_reset=factory_reset, log=self._log)
        self._apply_artifact_state(artifacts)
        self._set_status("Build complete")

    def _flash_serial(self) -> None:
        build_dir = self.current_build.build_dir if self.current_build else (Path(self.config_data.build_root) / "standard")
        self.service.upload_serial(self._current_port(), build_dir, self._log)
        self._set_status("Serial flash complete")

    def _flash_dfu(self) -> None:
        package = self.package_path.get().strip()
        if not package and self.current_build and self.current_build.zip_path:
            package = str(self.current_build.zip_path)
        if not package:
            raise RuntimeError("No DFU package available. Build first or specify a package path.")
        port = self._resolve_dfu_port()
        self._log(f"Using DFU port {port}")
        self.service.flash_dfu_serial(port, Path(package), self._log)
        self._set_status("DFU flash complete")

    def _build_and_flash_dfu(self) -> None:
        self._build(False)
        self._flash_dfu()

    def _reboot_puck_to_bootloader(self, *, serial_only: bool) -> None:
        if self.pairing_service.available():
            pucks = self.pairing_service.list_pucks()
            puck = self._selected_pairing_puck() if self.pairing_puck_map else None
            if puck is None and pucks:
                puck = pucks[0]
            if puck is not None:
                self.pairing_service.reboot_to_bootloader(puck.serial, serial_only=serial_only)
                flavor = "serial DFU" if serial_only else "UF2 bootloader"
                self._log(f"Requested {flavor} reboot for puck {puck.serial}. The device should disconnect and re-enumerate.")
                self._set_status(f"Rebooting to {flavor}")
                self.expect_bootloader_after_refresh = True
                self.after(3500, self.refresh_devices)
                return
        status = self.detector.webusb.read_openpuck_status()
        if status:
            self.detector.webusb.reboot_to_bootloader(serial_only=serial_only)
            flavor = "serial DFU" if serial_only else "UF2 bootloader"
            self._log(f"Requested {flavor} reboot over WebUSB.")
            self._set_status(f"Rebooting to {flavor}")
            self.expect_bootloader_after_refresh = True
            self.after(3500, self.refresh_devices)
            return
        raise RuntimeError("No updated OpenPuck HID/WebUSB device is available for bootloader reboot. Flash the new firmware onto a reachable device first.")

    def _copy_uf2(self) -> None:
        if not self.current_build or not self.current_build.uf2_path:
            raise RuntimeError("No UF2 artifact available. Build first.")
        target = self.uf2_target.get().strip()
        if not target:
            raise RuntimeError("Set the bootloader drive path first, for example D:\\ or /Volumes/UF2BOOT.")
        self.service.copy_uf2(self.current_build.uf2_path, Path(target), self._log)
        self._set_status("UF2 copied")

    def _erase_all(self) -> None:
        self.service.erase_all_serial(self._current_port(), self._log)
        self._set_status("ERASE-ALL sent")

    def open_serial_monitor(self) -> None:
        SerialMonitorWindow(self, self._current_port(), self.config_data.serial_baud)

    def launch_panel(self) -> None:
        url = self.panel_server.start(
            self.config_data.python_path,
            Path(self.config_data.repo_root) / "docs",
            self.config_data.panel_port,
        )
        self._log(f"WebUSB panel launched at {url}")
        self._set_status("Panel server running")

    def stop_panel(self) -> None:
        self.panel_server.stop()
        self._set_status("Panel server stopped")

    def check_updates(self) -> None:
        self._run_task("Check updates", self._check_updates_inner)

    def _check_updates_inner(self) -> None:
        head_file = Path(self.config_data.repo_root)
        current = "unknown"
        git_head = head_file / ".git"
        if git_head.exists():
            import subprocess

            current = subprocess.check_output(
                ["git", "rev-parse", "--short=8", "HEAD"],
                cwd=self.config_data.repo_root,
                text=True,
                encoding="utf-8",
            ).strip()
        info = self.updates.check_github_commit(current, self.config_data.update_source)
        if not info:
            self.after(0, lambda: self.update_status.set("No update source"))
            return
        message = (
            f"Update available: {info.latest} (current {info.current})"
            if info.available
            else f"Up to date: {info.current}"
        )
        self._log(message)
        self.after(0, lambda: self.update_status.set(message))
        if info.available:
            self._log(f"Review upstream at {info.url}")

    def _run_task(self, label: str, func) -> None:
        self._log(f"[task] {label}")
        self.active_task_count += 1
        thread = threading.Thread(target=self._task_wrapper, args=(func,), daemon=True)
        thread.start()

    def _task_wrapper(self, func) -> None:
        try:
            func()
        except Exception as exc:
            self._log(f"[error] {exc}")
            self._set_status("Task failed", color=ROSE)
        finally:
            self.active_task_count = max(0, self.active_task_count - 1)

    def _set_status(self, text: str, *, color: str | None = None) -> None:
        chosen = color or CYAN
        self.after(0, lambda: (self.status_chip.configure(text=text, text_color=chosen),))

    def _log(self, line: str) -> None:
        self.log_queue.put(line)

    def _drain_logs(self) -> None:
        flushed = False
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_box.insert("end", f"{time.strftime('%H:%M:%S')}  {line}\n")
            self.log_box.see("end")
            flushed = True
        if flushed:
            self.log_box.update_idletasks()
        self.after(120, self._drain_logs)

    def _restore_artifact_state(self) -> None:
        artifacts = self.service.discover_existing_artifacts()
        if artifacts:
            self._apply_artifact_state(artifacts)

    def _apply_artifact_state(self, artifacts: BuildArtifacts) -> None:
        self.current_build = artifacts
        self.package_path.set(str(artifacts.zip_path or ""))
        artifact_text = (
            "Artifacts:\n"
            f"Build Dir: {self._compact_path(artifacts.build_dir)}\n"
            f"UF2: {self._compact_path(artifacts.uf2_path)}\n"
            f"ZIP: {self._compact_path(artifacts.zip_path)}"
        )
        self.after(0, lambda: self.artifact_label.configure(text=artifact_text))

    def _current_selected_device_id(self) -> str | None:
        selected = self.selected_device.get()
        if selected in self.device_map:
            device_id = self.device_map[selected].device
            self.selected_device_id = device_id
            self.preferred_device_id = device_id
            return device_id
        return self.selected_device_id

    def _record_for_device(self, device_id: str | None) -> PortRecord | None:
        if not device_id:
            return None
        for record in self.device_map.values():
            if record.device == device_id:
                return record
        return None

    def _display_name_for_device(self, device_id: str | None) -> str | None:
        record = self._record_for_device(device_id)
        return record.display_name if record else None

    def _preferred_display_name(self, devices: list[PortRecord]) -> str | None:
        if not devices:
            return None
        if self.expect_bootloader_after_refresh:
            record = next((item for item in devices if item.is_bootloader), None)
            if record:
                return record.display_name
        preferred = next((item for item in devices if item.is_openpuck and item.openpuck_mode == "Steam"), None)
        if preferred:
            return preferred.display_name
        preferred = next((item for item in devices if item.is_bootloader), None)
        if preferred:
            return preferred.display_name
        preferred = next((item for item in devices if item.is_supported), None)
        if preferred:
            return preferred.display_name
        return devices[0].display_name

    def _resolve_dfu_port(self) -> str:
        current = self._record_for_device(self._current_selected_device_id())
        if current and current.is_bootloader:
            return current.device
        bootloaders = [device for device in self.devices if device.is_bootloader]
        if len(bootloaders) == 1:
            self.selected_device_id = bootloaders[0].device
            self.preferred_device_id = bootloaders[0].device
            self.after(0, lambda: self.selected_device.set(bootloaders[0].display_name))
            return bootloaders[0].device
        if bootloaders:
            raise RuntimeError(
                "Multiple DFU-capable bootloader ports were detected. Select the bootloader-tagged device in Detected Boards and retry."
            )
        raise RuntimeError(
            "No serial DFU bootloader port is selected or detected. Reboot the puck to Serial DFU first and wait for a bootloader-tagged COM port to appear."
        )

    def _compact_path(self, value: Path | None) -> str:
        if value is None:
            return "none"
        text = str(value)
        return text if len(text) <= 68 else f"...{text[-65:]}"


def main() -> None:
    app = OpenPuckFlasherApp()
    app.mainloop()
