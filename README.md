# Luxorparts KDC-030D — reverse-engineering notes

Reverse-engineering my own **Luxorparts "Kamera för barn"** (pink kids' thermal-print
camera, Kjell art. 66331, model **KDC-030D**) to explain, and hopefully fix, a
**~1-second shutter lag**: pressing the shutter captures the frame roughly one second
*after* the press, in every lighting condition.

This repo holds the tooling and findings. It is defensive, educational RE of hardware I
own. **No vendor firmware is redistributed here** — the reference BootROM/firmware images
come from the upstream projects linked below; point `KDC_ROM_DIR` at your local copies.

> Status: **SOLVED — no case opening.** This unit's own firmware was dumped over USB (0xDA
> BootROM loader + a hand-ported, position-independent flasher payload with its own SPI
> driver — no vendor addresses, no SPI clip, no soldering). cmd0 returned JEDEC `5e4016`;
> cmd1 returned real flash (BLDR boot header at +4). A 32 KiB app dump confirmed the
> **shutter lag is a hard-coded ~1000-tick wait** in the capture routine: at RAM
> `0x02007b48` (flash ≈ `0x9f48`) `l.addi r18,r24,1000` sets `deadline = uptime() + 1000`,
> and the frame is grabbed only after that deadline — independent of exposure. See
> [How the USB dump works](#how-the-usb-dump-works) and [The cause](#the-shutter-lag-cause).

## How the USB dump works

No case opening, no SPI clip. The chip is an AppoTech/Buildwin loader-capable SoC, so:

1. **`0xDA`** (vendor SCSI) reboots the stock firmware into its on-chip **BootROM loader**
   (`1908:3319` "BuildWin Video050Loader"), confirmed by USB/INQUIRY fingerprint.
2. A small **position-independent flasher payload** (`tools/flstub.py` → `flasher.bin`, a
   hand-port of matu's `boot/flasher.c`) is written to SRAM `0x2000` and launched through
   the loader's `0xCB` "call" (it hands the payload its request block in `r3`). The payload
   drives the SPI controller **with raw controller registers only** — it needs no
   build-specific vendor function addresses, so it works on a stock camera.
3. The payload runs command 0 (controller init + JEDEC check) then command 1 (flash read)
   into its request block; the host reads the result back with a plain `0xCB` memory read.

**What made it finally work:** the payload must preserve the callee-saved registers
(`r14/r16/r18/r20`) it uses, or returning to the BootROM corrupts the loader and every
follow-up USB transfer hangs. Earlier hand-assembled attempts skipped that and failed in
six different ways; a compiled C payload preserves them automatically. The fix is in
`flstub.py`'s entry/exit. The port is validated offline against a modeled CPU+SPI+flash
(`tools/flsim.py`: terminates, returns the right bytes, only ever clocks `{0x9f,0x03,addr,0xFF}`).

## The device

| | |
|---|---|
| Product | Luxorparts Kamera för barn, pink — Kjell 66331 (blue = 66332), model KDC-030D |
| SoC | **AppoTech / Buildwin JT5295** (AX3295B family), OpenRISC 1000, little-endian, **no branch delay slots** (a taken branch skips the next instruction; `l.jal` links to PC+4) |
| Flash | 4 MiB SPI NOR (JEDEC `5e 40 16`) |
| BootROM | 32 KiB @ `0x00100000`; **byte-identical** to penguin-camera's `soc_boot_rom.bin` (sha256 `62d4dabe…`) |
| USB (stock) | `0219:3280` "GENERAL - AUDIO" → `1908:3283` "Buildwin Media-Player"; SCSI INQUIRY `Buildwin / Media-Player`; USB-SDK stamp `20160418V1.000` |
| USB (loader) | `1908:3319` "BLDR v1.00" / "BuildWin Video050Loader" — reached by vendor SCSI `0xDA` |
| SD card | `PHOTO/PHO#####.JPG` (9280×5160 interpolated, no EXIF), `VIDEO/`, `MP3/`. Plugging USB shows the card as mass storage and blanks the screen — normal, not a fault. |

## The shutter-lag cause

Found by **offline** disassembly of penguin-camera's AX3295B stock firmware (a build on
the same SoC + USB SDK as this camera — but *not* a dump of this unit, so this is a
strong hypothesis, not proof for our exact build):

The capture routine (RAM `0x02007460`) takes a start timestamp, then **blocks for a fixed
~1000 ms** before grabbing the frame:

```
r24 = uptime()                 # ms counter @ 0x0208671c   (0x02007908)
deadline = r24 + 1000          #                            (0x02007a54)
while uptime() < deadline: delay()   # fixed wait           (0x02007a58..0x02007a80)
# only now: sensor grab + JPEG encode                        (0x02007a84+)
```

So the lag is a **hard-coded delay**, not auto-exposure settling — which matches the
symptom being constant across lighting. A real fix means patching that constant and
reflashing, which requires writing flash (out of scope until the read-only dump is done
and the risk accepted).

## Layout

```
tools/
  or1k_dis.py     minimal little-endian OpenRISC (ORBIS32) disassembler
  flstub.py       the working flasher payload: a little-endian hand-port of matu's
                  boot/flasher.c (cmd0 init+JEDEC, cmd1 flash read) -> flasher.bin.
                  Needs no OpenRISC toolchain; its Asm emits LE words directly.
  flsim.py        offline validator: runs flasher.bin on a modeled CPU+SPI+flash and
                  checks it terminates, reads the right bytes, clocks only {0x9f,0x03,addr,0xFF}
  stub_build.py   assembles the marker/reader/diag probe payloads (Asm lives here)
  spi_trace.py    emulates the ROM SPI-read path; logs every byte clocked to TX (SPR 0x9250)
  spi_frame.py    emulates it again; proves chip-select stays asserted for one transaction
  stub_*.bin      small probe payloads (marker: zero-SPI launch test; reader/diag: experiments)
device/
  kdc_dump.py     loader entry + USB/BootROM fingerprinting + 0xCB memory read
  kdc_run.py      flasher | dump | reader | marker | diag | stockread | bootrom
                  (flasher = the working read; dump = loop it over a range to a file)
```

Dump this unit's app region (needs the camera on its USB screen, SD volume unmounted):
```bash
sudo venv/bin/python device/kdc_run.py flasher 0 256              # proof: JEDEC + 256 bytes @ 0
sudo venv/bin/python device/kdc_run.py dump 0x8000 0x8000 app.bin # 32 KiB incl. the capture routine
```

## Safety model (why this can't brick the flash)

Every destructive SPI NOR operation (page program `0x02`, sector/block erase `0x20`/`0xD8`,
status-register write `0x01`) is ignored by the chip until a **Write-Enable (`0x06`, WREN)**
sets the write-enable latch. The reader payload (`stub_reader.bin`) **never emits `0x06`** —
that opcode is not present in its code at all; `stub_build.py` scans the assembled bytes and
confirms the only values ever sent to the TX register are `0x03` (READ), address bytes, and
`0xFF`. Emulation (`spi_trace.py`, `spi_frame.py`) shows the same on the ROM read path and
that chip-select is held for a single transaction, so address bytes are operands of the READ,
never re-framed as commands. Consequently the worst realistic failure is a **bus hang that a
power-cycle clears** (the loader watchdog resets the chip to stock after ~2 s), not a brick.

This was independently reviewed; the review caught a real bug (an invalid `l.nop` encoding
`0x54000000` instead of `0x15000000`) which is fixed.

## Current status

Proven **on-device** (read-only):
- `0xDA` enters the BootROM loader; its USB/INQUIRY fingerprint matches the references.
- This unit's 32 KiB BootROM is byte-identical to penguin-camera's (so the analyzed ROM
  code applies here exactly).
- The loader can accept an uploaded SRAM payload and launch it: **`stub_marker.bin` ran and
  returned cleanly** (wrote `0xCAFE`), confirming SRAM write + launch + return.

Remaining:
- The read-only `stub_reader.bin` currently **hangs** because the SPI controller needs a
  one-time clock/controller init in loader mode (`SPR 0x9000 = 0x01115005` plus clock-gate
  writes — the exact sequence is in matu's `rom-rescue.c`, verified on hardware) that a bare
  gate-enable doesn't provide. Porting that init (read-only) is the next step. A hang here is
  recoverable by power-cycle; no flash write is possible (see safety model).

## Running (macOS)

```bash
python3 -m venv venv && venv/bin/pip install pyusb
export KDC_LIBUSB=/opt/homebrew/lib/libusb-1.0.dylib     # libusb for pyusb
export KDC_ROM_DIR=/path/to/reference/roms               # holds soc_boot_rom.bin (+ optional bootrom_matu_64k.bin)
# camera powered on, connected, SD volume unmounted (diskutil unmountDisk /dev/diskN):
sudo venv/bin/python device/kdc_run.py bootrom our_bootrom.bin   # read-only: dump+verify BootROM
sudo venv/bin/python device/kdc_run.py marker                    # zero-SPI launch test
```
Emulation needs no hardware (point `KDC_PENGUIN_REPO` at a penguin-camera checkout for its
`or1k_subset.py`):
```bash
KDC_PENGUIN_REPO=/path/to/penguin-camera KDC_ROM_DIR=/path/to/roms python3 tools/spi_trace.py
python3 tools/stub_build.py        # (re)assemble the stubs; prints the TX opcode allowlist
```

## Credits / upstream (reference images live there, not here)

- [matu6968/Action_Digital_Kids_Camera_DOOM](https://github.com/matu6968/Action_Digital_Kids_Camera_DOOM) — JT5295 SoC id, loader protocol, `rom-rescue.c`, `or1k_subset`-style tooling.
- [hidoba/penguin-camera](https://github.com/hidoba/penguin-camera) — AX3295B custom firmware, BootROM image, `or1k_subset.py`.
- [GainSec: reverse engineering the MISIRUN instant-print camera](https://gainsec.com/2025/04/01/reverse-engineering-kids-instaprint-camera/) and the [Habr AX3295B teardown](https://habr.com/ru/companies/ruvds/articles/779626/).

## License

MIT for the code in this repo (my own work). The referenced vendor firmware/BootROM images
are **not** included and remain their respective owners'.
