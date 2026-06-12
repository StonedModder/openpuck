# pairtui — steamless pairing + bond inspector (macOS)

A small terminal UI for the Valve SC2 ("Triton") puck / copycat. It reads the puck's 4 bond slots over USB-HID
and can pair / unpair controllers **without Steam**. It also surfaces the **`ibex_uuid`** of each bond — which is
exactly what the RF sniffer (`../puck_sniffer/`, `../docs/sniffer.html`) needs for its **lock ibex** field.

It talks to the **vendor HID interface** (usage page `0xFF00`), which opens **without** macOS Input-Monitoring
permission, via the helper binary `scmd`.

## Build (macOS)
`pairtui.py` is pure Python stdlib (`curses`, no pip installs). Its only dependency is `scmd`, built from
`scmd.c` with the IOKit + CoreFoundation frameworks:

```sh
clang -framework IOKit -framework CoreFoundation -o scmd scmd.c
```

(macOS only — `scmd` uses IOKit HID. There is no Linux/Windows port here.)

## Run
```sh
python3 pairtui.py
```

Keys: `↑↓` select slot · `p` pair · `u` unpair · `F` wipe all · `P` next puck · `r` rescan · `i` refresh · `q` quit.

The table shows each slot's **state** (CONNECTED / CONNECTING / BONDED / empty — live `ibexN_state` is read from
the real puck's `dongle~$` CDC shell when present), the bonded **controller serial**, and the **IBEX UUID**.

## What you need it for here
- **Sniffer lock:** a *connected* slot doesn't beacon on ch2, so the sniffer locks onto a bonded slot by its
  `ibex_uuid`. Read that uuid here (e.g. `0xEF7171B4`) and type it into the sniffer app's **lock ibex** field.
  The uuid is the bond id — **stable across reconnects** (the on-air address is random per session, the uuid is not).
- **Labelling captures:** confirms which controller/slot a capture belongs to.

## Bond record (read via `0xA3`, `--up 1 --idx 0..3`)
24 bytes per slot:

```
[0..3]  proteus_uuid   (u32, little-endian)
[4..7]  ibex_uuid      (u32, little-endian)   <- the value the sniffer locks on
[8..23] controller serial (ASCII, NUL-padded)
```

## scmd (reference)
`scmd` is a thin HID feature-report tool. Safe (read-only) commands are used by default; destructive opcodes
(`0x86` reset, `0x9F` off, `0xAD` pair, …) are only sent when passed explicitly.

```sh
./scmd list                                   # list Valve HID nodes + serials
./scmd 1304 --serial <S> --up 1 --idx 0 A3    # read bond slot 0 of puck <S>  (1304 = puck PID)
```

pairtui drives `scmd` for all reads/writes; you normally won't call it directly.

## Safety note
`pairtui` only sends `connection` to a shell it has confirmed is the **real puck** (`dongle~$` prompt); it hands
off harmlessly if it sees the copycat's different single-letter CDC shell.
