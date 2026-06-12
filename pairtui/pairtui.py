#!/usr/bin/env python3
"""pairtui — steamless pairing manager for the Steam Controller 2 ("Triton") puck / copycat.

Reads the 4 bond slots over USB-HID (0xA3 on control interface --up 1 --idx 0..3, the same way Steam
reads dongle slots — one bond per interface). Pair / unpair go via ./scmd over USB-HID; live connection
state (CONNECTED/CONNECTING/IDLE) is read from the real puck's `dongle~$` CDC shell when present.

  scmd: 0xA2 puck slot write/clear, 0xEE/0xEF controller esb/bond, 0x95 reboot-to-wireless, 0xAD pairing.
  Build scmd:  clang -framework IOKit -framework CoreFoundation -o scmd scmd.c
  Run: python3 pairtui.py        (pure stdlib: curses + termios)
"""
import curses, subprocess, re, os, glob, termios, time, select

HERE = os.path.dirname(os.path.abspath(__file__))
SCMD = os.path.join(HERE, "scmd")
PUCK_PID = "1304"          # Valve puck / copycat
CTRL_PIDS = ("1301", "1302", "1303", "1205")  # Steam Controller variants (NOT a puck)


def scmd(args, timeout=30):
    try:
        return subprocess.run([SCMD] + args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:
        return "ERR %s" % e


def _list_devices():
    """Parse `scmd list` -> dedup dict {serial: {pid, product}}. One entry per physical device."""
    out = scmd(["list"], timeout=8)
    devs = {}
    for m in re.finditer(r"Valve 28DE:([0-9A-Fa-f]+)\b.*?serial=(\S+)\s+product=(.+?)\s+manufacturer", out):
        pid, ser, prod = m.group(1).lower(), m.group(2), m.group(3).strip()
        if ser not in devs:
            devs[ser] = {"pid": pid, "product": prod}
    return devs


def find_pucks():
    """[(serial, cdc_port)] for PID-1304 PUCKS ONLY (a controller is PID 1302 and is excluded)."""
    res = []
    for ser, d in _list_devices().items():
        if d["pid"] != PUCK_PID:
            continue
        port = (glob.glob("/dev/cu.usbmodem%s*" % ser) or [None])[0]
        res.append((ser, port))
    return res


def find_controllers():
    """[(pid, serial)] of docked Valve controllers (anything that is NOT a puck)."""
    res = []
    for ser, d in _list_devices().items():
        if d["pid"] == PUCK_PID or "Puck" in d["product"]:
            continue
        if ser.startswith("FX"):
            res.append((d["pid"], ser))
    return res


class Shell:
    """Talk to the real puck's CDC debug shell (dongle~$). Used only for live `connection` state."""
    def __init__(self, port): self.port = port; self.fd = None
    def open(self):
        self.fd = os.open(self.port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        a = termios.tcgetattr(self.fd); a[4] = a[5] = termios.B115200
        a[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        a[0] &= ~(termios.IXON | termios.ICRNL); a[1] &= ~termios.OPOST
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        self.cmd("", 0.3)
    def close(self):
        if self.fd is not None:
            try: os.close(self.fd)
            except Exception: pass
            self.fd = None
    def cmd(self, c, wait=0.8):
        os.write(self.fd, (c + "\r\n").encode()); out = b""; t0 = time.time()
        while time.time() - t0 < wait:
            if select.select([self.fd], [], [], 0.1)[0]:
                try: out += os.read(self.fd, 8192)
                except Exception: pass
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.decode("ascii", "replace"))


def shell_connection(port):
    """Return {slot_idx: STATE} from the real puck's `connection` command, or {} otherwise.

    SAFETY: the copycat's CDC is a *different* command shell (single-letter cmds like 'c'=channel), so
    blindly sending "connection" there would be misread as a command and corrupt its state. We first
    probe with a harmless newline and ONLY proceed if the reply shows the real puck's `dongle~$` prompt."""
    if not port:
        return {}
    sh = Shell(port)
    try:
        sh.open()
        probe = sh.cmd("", 0.3)                      # bare newline — harmless on any shell
        if "dongle" not in probe and "~$" not in probe:
            return {}                                # not the dongle shell (e.g. the copycat): hands off
        txt = sh.cmd("connection", 0.7)
    except Exception:
        return {}
    finally:
        sh.close()
    states = {}
    for m in re.finditer(r"ibex(\d)_state\s*:\s*(\w+)", txt):
        states[int(m.group(1))] = m.group(2).upper()
    return states


def ctrl_read(puck_serial, idx):
    """Read 0xA3 (bond/status) from control interface --up 1 --idx; return (serial, raw_record_hex)."""
    out = scmd([PUCK_PID, "--serial", puck_serial, "--up", "1", "--idx", str(idx), "A3"], timeout=8)
    m = re.search(r"ascii:.*?(FX[A-Z0-9]{6,})", out)
    serial = m.group(1) if m else ""
    rm = re.search(r"02 A3 [0-9A-Fa-f]{2} ([0-9A-Fa-f ]+)", out)
    return serial, (rm.group(1).strip() if rm else "")


def _ser16(serial):
    b = serial.encode()[:16]; b += b"\x00" * (16 - len(b))
    return ["%02x" % x for x in b]


def pair_full(puck_serial, puck_slot, ctrl_pid, ctrl_serial):
    """Steamless pair (controller must be USB-docked). Fresh 8-byte uuid; writes the puck slot
    (0xA2 [uuids][ctrl_serial]) and the controller (0xEE esb/bond [uuids][puck_serial] + 0xEF commit),
    then 0x95 reboots the controller to wireless. Undock to connect. Returns the uuids hex string."""
    uuids = ["%02x" % x for x in os.urandom(8)]
    key = ["%02x" % x for x in b"esb/bond\x00"]
    scmd([PUCK_PID, "--serial", puck_serial, "--up", "1", "--idx", str(puck_slot), "A2"]
         + uuids + _ser16(ctrl_serial))
    scmd([ctrl_pid, "--serial", ctrl_serial, "--up", "1", "EE"] + key + uuids + _ser16(puck_serial))
    scmd([ctrl_pid, "--serial", ctrl_serial, "--up", "1", "EF"] + key)
    scmd([ctrl_pid, "--serial", ctrl_serial, "--up", "1", "95", "52", "af", "27", "a4"])
    return "".join(uuids)


def usb_slots(puck_serial):
    """Read all 4 bond slots over USB (0xA3, --up 1 --idx 0..3). Works for real puck and copycat.
    Record = [4B proteus_uuid LE][4B ibex_uuid LE][16B controller serial]."""
    slots = []
    for i in range(4):
        _, rec = ctrl_read(puck_serial, i)
        b = bytes(int(x, 16) for x in rec.split()[:24]) if rec else b""
        used = len(b) >= 24 and any(b[0:8])
        slots.append({
            "idx": i,
            "puuid": "0x%08X" % int.from_bytes(b[0:4], "little") if used else "",
            "iuuid": "0x%08X" % int.from_bytes(b[4:8], "little") if used else "",
            "serial": b[8:24].split(b"\x00")[0].decode("latin1") if used else "",
            "state": "BONDED" if used else "empty",
        })
    return slots


# ───────────────────────────── UI ─────────────────────────────
CP_TITLE, CP_OK, CP_CONN, CP_WARN, CP_DIM, CP_SEL, CP_KEY, CP_HDR = range(1, 9)

STATE_STYLE = {
    "CONNECTED": (CP_CONN, "●"), "CONNECTING": (CP_WARN, "◐"),
    "BONDED": (CP_OK, "●"), "IDLE": (CP_DIM, "○"), "empty": (CP_DIM, "·"),
}


class TUI:
    def __init__(self, scr):
        self.scr = scr; self.pucks = []; self.pi = 0; self.slot_i = 0
        self.slots = []; self.conn = {}; self.log = ["ready"]
        self.rescan()

    def msg(self, m):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), m)); self.log = self.log[-300:]

    def cur(self):
        return self.pucks[self.pi] if self.pucks else None

    def rescan(self):
        self.pucks = find_pucks(); self.pi = min(self.pi, max(0, len(self.pucks) - 1))
        self.msg("found %d puck(s), %d controller(s)" % (len(self.pucks), len(find_controllers())))
        self.refresh()

    def refresh(self):
        p = self.cur()
        if not p:
            self.slots = []; self.conn = {}; return
        try:
            self.slots = usb_slots(p[0])
            self.conn = shell_connection(p[1])  # {} unless it's a real puck dongle shell
            n = sum(1 for s in self.slots if s["serial"])
            self.msg("%s: %d bonded%s" % (p[0], n, "  (live state)" if self.conn else ""))
        except Exception as e:
            self.msg("read err: %s" % e); self.slots = []

    def slot_state(self, i):
        """Prefer live connection state from the shell; fall back to the bonded/empty USB read."""
        sl = self.slots[i] if i < len(self.slots) else {"state": "empty", "serial": "", "iuuid": ""}
        st = self.conn.get(i, sl["state"])
        if st == "IDLE" and not sl["serial"]:
            st = "empty"
        return st, sl

    # ---- drawing helpers ----
    def _cs(self, cp):
        return curses.color_pair(cp) if curses.has_colors() else 0

    def draw(self):
        s = self.scr; s.erase(); h, w = s.getmaxyx(); W = max(40, w)
        def ln(y, x, t, a=0):
            if 0 <= y < h: s.addnstr(y, x, t, max(0, w - x - 1), a)
        def rule(y, label=""):
            if 0 <= y < h:
                s.hline(y, 0, curses.ACS_HLINE, w - 1)
                if label: ln(y, 3, " %s " % label, self._cs(CP_HDR) | curses.A_BOLD)

        # title bar
        title = " ⬡  TRITON  ·  steamless pairing manager "
        ln(0, 0, title.ljust(w - 1), self._cs(CP_TITLE) | curses.A_BOLD)

        # device line
        p = self.cur()
        if p:
            anyconn = any(v in ("CONNECTED", "CONNECTING") for v in self.conn.values())
            badge = "● live" if self.conn else "○ usb"
            ln(2, 2, "PUCK", self._cs(CP_KEY) | curses.A_BOLD)
            ln(2, 8, "%d/%d" % (self.pi + 1, len(self.pucks)), curses.A_DIM)
            ln(2, 14, p[0], curses.A_BOLD)
            ln(2, 14 + len(p[0]) + 3, badge, self._cs(CP_CONN if anyconn else CP_DIM))
            if p[1]:
                ln(2, w - len(p[1]) - 3, p[1].split("/")[-1], curses.A_DIM)
        else:
            ln(2, 2, "no puck found — plug in the copycat / dongle, press [r]", self._cs(CP_WARN))

        # slot table
        rule(4, "bond slots")
        ln(5, 2, "  %-4s %-12s %-18s %-12s" % ("SLOT", "STATE", "CONTROLLER", "IBEX UUID"),
           self._cs(CP_DIM) | curses.A_BOLD)
        for i in range(4):
            st, sl = self.slot_state(i)
            cp, glyph = STATE_STYLE.get(st, (CP_DIM, "·"))
            sel = (i == self.slot_i)
            who = sl["serial"] or "—"
            cursor = "▶" if sel else " "
            base_a = self._cs(CP_SEL) | curses.A_BOLD if sel else 0
            ln(6 + i, 1, "%s ibex%d %s %-11s %-18s %-12s"
               % (cursor, i, glyph, st.lower(), who, sl.get("iuuid", "")), base_a)

        # controllers available
        rule(11, "docked controllers")
        ctrls = find_controllers()
        ln(12, 2, "  ".join("%s" % c[1] for c in ctrls) or "(none docked)",
           self._cs(CP_OK) if ctrls else self._cs(CP_DIM))

        # log
        rule(14, "log")
        for i, m in enumerate(self.log[-(h - 18):]):
            ln(15 + i, 2, m, curses.A_DIM)

        # footer key bar
        keys = [("↑↓", "slot"), ("p", "pair"), ("u", "unpair"), ("F", "wipe"),
                ("P", "puck"), ("r", "rescan"), ("i", "refresh"), ("q", "quit")]
        x = 1
        for k, lbl in keys:
            seg = " %s " % k
            if x + len(seg) + len(lbl) + 2 < w:
                s.addnstr(h - 1, x, seg, len(seg), self._cs(CP_KEY) | curses.A_REVERSE | curses.A_BOLD)
                x += len(seg)
                s.addnstr(h - 1, x, " %s " % lbl, len(lbl) + 2, self._cs(CP_DIM))
                x += len(lbl) + 2
        s.refresh()

    def prompt(self, msg, color=CP_WARN):
        h, w = self.scr.getmaxyx(); curses.echo()
        self.scr.addnstr(h - 1, 0, (msg + " ").ljust(w - 1), w - 1,
                         self._cs(color) | curses.A_REVERSE | curses.A_BOLD)
        self.scr.refresh()
        try: v = self.scr.getstr(h - 1, len(msg) + 1, 60).decode().strip()
        except Exception: v = ""
        curses.noecho(); return v

    def do_pair(self):
        p = self.cur(); i = self.slot_i
        if not p: return
        if i < len(self.slots) and self.slots[i]["serial"]:
            self.msg("slot %d occupied — pick a vacant slot or unpair first" % i); return
        ctrls = find_controllers()
        if not ctrls:
            self.msg("no USB-docked controller found — plug one in to pair"); return
        pid, cser = ctrls[0]
        if len(ctrls) > 1:
            pick = self.prompt("controllers %s — serial to pair:" % ",".join(c[1] for c in ctrls)).strip()
            for cp, cs in ctrls:
                if cs == pick: pid, cser = cp, cs; break
        if self.prompt("pair %s -> ibex%d ?  type YES:" % (cser, i)) != "YES":
            self.msg("pair cancelled"); return
        uu = pair_full(p[0], i, pid, cser)
        self.msg("paired %s -> ibex%d (uuid %s) — UNDOCK controller to connect" % (cser, i, uu))
        time.sleep(1); self.refresh()

    def do_unpair(self):
        p = self.cur(); i = self.slot_i
        if not p or i >= len(self.slots) or not self.slots[i]["serial"]:
            self.msg("slot %d empty" % i); return
        target = self.slots[i]["serial"]
        if self.prompt("clear ibex%d (%s) ?  type YES:" % (i, target)) != "YES":
            self.msg("unpair cancelled"); return
        scmd([PUCK_PID, "--serial", p[0], "--up", "1", "--idx", str(i), "A2"] + ["00"] * 24, timeout=12)
        self.msg("forget sent (slot %d)" % i); time.sleep(0.3); self.refresh()

    def do_factory(self):
        p = self.cur()
        if not p: return
        if self.prompt("wipe ALL slots ?  type WIPE:") != "WIPE":
            self.msg("wipe cancelled"); return
        for i in range(4):
            scmd([PUCK_PID, "--serial", p[0], "--up", "1", "--idx", str(i), "A2"] + ["00"] * 24, timeout=12)
        self.msg("wiped all slots"); time.sleep(0.5); self.refresh()

    def run(self):
        while True:
            self.draw(); k = self.scr.getch()
            if k in (ord("q"), 27): break
            elif k in (curses.KEY_UP, ord("k")): self.slot_i = (self.slot_i - 1) % 4
            elif k in (curses.KEY_DOWN, ord("j")): self.slot_i = (self.slot_i + 1) % 4
            elif k == ord("P") and self.pucks:
                self.pi = (self.pi + 1) % len(self.pucks); self.refresh()
            elif k == ord("p"): self.do_pair()
            elif k == ord("u"): self.do_unpair()
            elif k == ord("F"): self.do_factory()
            elif k == ord("i"): self.refresh()
            elif k == ord("r"): self.rescan()


def main(scr):
    curses.curs_set(0); scr.keypad(True)
    if curses.has_colors():
        curses.start_color(); curses.use_default_colors()
        curses.init_pair(CP_TITLE, curses.COLOR_CYAN, -1)
        curses.init_pair(CP_OK,    curses.COLOR_GREEN, -1)
        curses.init_pair(CP_CONN,  curses.COLOR_GREEN, -1)
        curses.init_pair(CP_WARN,  curses.COLOR_YELLOW, -1)
        curses.init_pair(CP_DIM,   curses.COLOR_WHITE, -1)
        curses.init_pair(CP_SEL,   curses.COLOR_CYAN, -1)
        curses.init_pair(CP_KEY,   curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(CP_HDR,   curses.COLOR_MAGENTA, -1)
    TUI(scr).run()


if __name__ == "__main__":
    if not os.path.exists(SCMD):
        raise SystemExit("build scmd: clang -framework IOKit -framework CoreFoundation -o scmd scmd.c")
    curses.wrapper(main)
