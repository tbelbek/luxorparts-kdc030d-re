# Luxorparts KDC-030D — reverse-engineering & shutter-lag fix

Reverse-engineering my own **Luxorparts "Kamera för barn"** (pink kids' thermal-print
camera, Kjell art. **66331**, model **KDC-030D**) to explain and **fix** a
**~1-second shutter lag**: pressing the shutter captured the frame roughly one second
*after* the press, in every lighting condition.

This repo holds the tooling and findings. It is defensive, educational RE of hardware I
own. **No vendor firmware, BootROM or extracted assets are redistributed here** — the
reference images come from the upstream projects linked below; point the env vars at your
own local copies.

> **Status: SOLVED and FIXED — no case opening, no SPI clip, no soldering.**
> This unit's firmware was dumped over USB, the lag was located in the capture routine, and
> the fix was flashed back and **verified on the device** (shutter lag ~1 s → ~¼ s, exposure
> unchanged). The whole read→patch→write→verify path runs through the stock USB port.

## The device

| | |
|---|---|
| Product | Luxorparts Kamera för barn, pink — Kjell 66331 (blue = 66332), model **KDC-030D** |
| SoC | **AppoTech / Buildwin JT5295** (AX3295B family), **OpenRISC 1000**, little-endian, **no branch delay slot** (a taken branch skips the next instruction; `l.jal` links r9 = PC+4) |
| Flash | **4 MiB SPI NOR, Zbit ZB25VQ32** — JEDEC `5e 40 16` (0x5e = Zbit). 4 KiB sector erase `0x20`, 256 B page program `0x02`, 64 KiB block erase `0xd8` |
| BootROM | 32 KiB @ `0x00100000`; **byte-identical** to penguin-camera's `soc_boot_rom.bin` (sha256 `62d4dabe…`) |
| USB (stock) | `0219:3280` "GENERAL - AUDIO" → `1908:3283` "Buildwin Media-Player"; USB-SDK stamp `20160418V1.000` |
| USB (loader) | `1908:3319` "BLDR v1.00" / "BuildWin Video050Loader" — reached by vendor SCSI `0xDA` |
| SD card | `PHOTO/`, `VIDEO/`, `MP3/`. Plugging USB shows the card as mass storage and blanks the screen — normal. |

## Firmware memory map (from the dump)

```
0x000000..0x0001ff  BootROM header: "BLDR" at +4, load address 0x02000000 at +0x20,
                     start sector 18 (0x2400) at +0x24, load sector count (995) at +0x28,
                     "SFAT" asset-table marker at +0x304
0x000200..0x0023ff  vendor SRAM bootstrap (PLL/DDR calibration, vector table)
0x002400..0x07ea00  application CODE — loaded contiguously to RAM 0x02000000 (509 KiB)
0x07ea00..0x1ce000  DATA / assets (JPEG UI graphics, PCM sounds, bitmap font, SFAT dir)
0x1ce000..0x400000  blank (0xFF)
```
RAM↔flash for the app: `RAM = flash_offset + 0x01ffdc00` (i.e. flash `0x2400` → RAM `0x02000000`).

## The shutter-lag cause

Found by disassembling this unit's own app (`tools/or1k_dis.py`). The capture routine at
RAM `0x02007460` waits for several subsystem-ready signals and then blocks for a **fixed
~1000 ticks (tick ≈ 1 ms)** before grabbing the frame:

```
0x02007b48:  l.addi r18,r24,1000      ; deadline = timestamp + 1000   (word 0x9e5803e8, flash 0x9f48)
0x02007b4c…: uptime()/delay() loop until uptime >= deadline
0x02007b78:  l.jal 0x02007164         ; the actual frame grab
```

The wait body only calls `uptime()` (reads a ms counter at `0x02081284`) and `delay()` — no
sensor/ISP/DMA access — so it is a pure fixed settle (AE/AWB convergence margin), independent
of exposure, which matches the symptom being constant across lighting. The capture routine
already waits on readiness flags *before* this, so the fixed 1000 is extra margin. **The fix
lowers that one constant** (e.g. 1000 → 250) — two bytes at flash `0x9f48`.

## How the USB read works

No case opening, no SPI clip:

1. **`0xDA`** (vendor SCSI) reboots the stock firmware into its on-chip **BootROM loader**
   (`1908:3319`), confirmed by USB/INQUIRY fingerprint.
2. A small **position-independent payload** (`tools/flstub.py`, a little-endian hand-port of
   matu's `boot/flasher.c`) is written to SRAM `0x2000` and launched through the loader's
   `0xCB` "call" (it hands the payload its request block in `r3`). The payload drives the SPI
   controller **with raw controller registers only** — no build-specific vendor addresses, so
   it runs on a stock camera.
3. Request commands: `0` init+JEDEC, `1` flash read, `2` page program, `3` sector erase. The
   host (`device/kdc_run.py`) builds the request, launches, and reads the result back.

**What made it work:** the payload must preserve the callee-saved registers it uses
(`r14/r16/r18/r20`) or returning to the BootROM corrupts the loader and USB hangs. The port
is validated offline against a modeled CPU+SPI+flash (`tools/flsim.py`).

## The fix (write path)

The write payload (`flstub.build(window=(lo,hi))`) is **compile-time restricted** to a single
address window, so it can physically only erase/program inside it. `device/kdc_run.py` drives
the fix with heavy verification; **nothing is written unless the final argument is `WRITE`**:

```bash
# DRY RUN — reads only (JEDEC + the target sector), writes nothing:
sudo python3 device/kdc_run.py flashpatch YOUR-4MB-DUMP.bin 250
# COMMIT — erase + 16-page program + readback verify of sector 0x9000:
sudo python3 device/kdc_run.py flashpatch YOUR-4MB-DUMP.bin 250 WRITE
# RECOVERY — rewrite sector 0x9000 from the dump (stock value):
sudo python3 device/kdc_run.py flashrestore YOUR-4MB-DUMP.bin WRITE
```

`250` is the new tick value (≈ 250 ms settle). Re-run with a different number to tune; the
reflash is cheap, off the boot path, and recoverable.

Two more patch modes share the same safe write path (dry-run unless the final arg is `WRITE`):

```bash
# silence the power-on/off jingles (zeros the two longest WAVs' PCM samples; headers + other sounds kept):
sudo python3 device/kdc_run.py silence-sounds YOUR-4MB-DUMP.bin WRITE
```

`tools/xref.py` builds a call-graph from an `or1k_dis.py` listing (`callers`/`calls`/`trace`/`func`)
so you can see "where is what called" — standard decompilers mis-model this SoC's call/branch slots,
so this works off the correct listing instead.

Safety model, in order:
- **Full 4 MiB backup first** (`dump 0 0x400000`), then every write verifies against it.
- **Erase-granule preflight** (committed writes only): in blank flash at `0x200000`, program a
  marker into two adjacent 4 KiB sectors, erase one, and confirm the neighbour survives —
  proving opcode `0x20` is a true 4 KiB sector erase on *this* chip before touching `0x9000`
  (which shares a 64 KiB block with the boot sector). Zero risk: the scratch block is blank.
- **Live == backup gate**: refuses to patch unless the on-chip sector still matches the backup.
- **Window-guarded payload**: erase/program outside the target window is rejected in the payload.
- **Readback verify** after erase (all-`0xFF`) and after program (byte-exact).
- **Recovery is case-free**: the patch is off the boot/USB path, so a bad value still boots and
  re-enters the `0xDA` loader; `flashrestore` rewrites the sector from the backup.

## Safety — why this cannot brick the flash easily

Every destructive SPI op (program `0x02`, erase `0x20`/`0xd8`, status-write `0x01`) is ignored
by the chip until a **Write-Enable (`0x06`)** sets the WEL latch. The read payload emits only
`{0x9f,0x03,addr,0xFF}`; the write payload adds only `{0x06,0x05,0x20,0x02}` and nothing else
(`tools/flstub.py` prints the exact TX opcode set on build). `tools/flsim.py` asserts, offline,
that no opcode outside the allow-list is ever clocked, that out-of-window/misaligned requests
are rejected, that a transfer timeout reports failure (not a false success), and that a full
erase→program→readback changes exactly the two patched bytes.

## Layout

```
device/
  kdc_dump.py   loader entry + USB/BootROM fingerprinting + 0xCB memory read/write
  kdc_run.py    flasher | dump | flashpatch | flashrestore | silence-sounds | reader | marker | diag | bootrom
tools/
  flstub.py     the payload: LE hand-port of flasher.c (cmd 0/1/2/3) -> in-memory blob,
                build(window=(lo,hi)) hard-restricts erase/program to that window
  xref.py       call-graph / cross-reference index over an or1k_dis.py listing (who-calls-what)
  flsim.py      offline validator: CPU+SPI+flash model; runs the full write flow, the bounds
                guards, and fault injection. Runs on a synthetic image with no dump present.
  or1k_dis.py   minimal little-endian OpenRISC (ORBIS32) disassembler
  stub_build.py the Asm (LE OpenRISC assembler) + marker/reader/diag probe payloads
  spi_trace.py  / spi_frame.py   emulate the ROM SPI-read path (CS framing, TX opcode log)
  stub_*.bin    small probe payloads (marker: zero-SPI launch test; reader/diag: experiments)
```

## Running (macOS)

```bash
python3 -m venv venv && venv/bin/pip install pyusb
export KDC_LIBUSB=/opt/homebrew/lib/libusb-1.0.dylib      # libusb for pyusb (default shown)
export KDC_PENGUIN_REPO=/path/to/penguin-camera           # for tools/flsim.py (its or1k_subset)
export KDC_DUMP=/path/to/your-4mb-dump.bin                # optional: real dump for flsim

# camera powered on, connected, SD volume unmounted (diskutil unmountDisk /dev/diskN):
sudo venv/bin/python device/kdc_run.py dump 0 0x400000 full_backup_4mb.bin   # full backup FIRST
sudo venv/bin/python device/kdc_run.py flashpatch full_backup_4mb.bin 250    # dry run
sudo venv/bin/python device/kdc_run.py flashpatch full_backup_4mb.bin 250 WRITE

# offline self-test (no hardware; needs KDC_PENGUIN_REPO, dump optional):
KDC_PENGUIN_REPO=/path/to/penguin-camera python3 tools/flsim.py
```

The camera returns to stock USB mode (`0219:3280`) after each loader run via its watchdog; if a
command reports "camera not found in stock USB mode", power-cycle / re-plug and retry.

See [AGENTS.md](AGENTS.md) for a contributor/agent orientation (chipset facts, ISA quirks,
the exact workflow and the rules that keep writes safe).

## Credits / upstream (reference images live there, not here)

- [matu6968/Action_Digital_Kids_Camera_DOOM](https://github.com/matu6968/Action_Digital_Kids_Camera_DOOM) — JT5295 SoC id, loader protocol, `boot/flasher.c`, `apxtool`.
- [hidoba/penguin-camera](https://github.com/hidoba/penguin-camera) — AX3295B custom firmware, BootROM image, `or1k_subset.py`.
- [GainSec: reverse engineering the MISIRUN instant-print camera](https://gainsec.com/2025/04/01/reverse-engineering-kids-instaprint-camera/) and the [Habr AX3295B teardown](https://habr.com/ru/companies/ruvds/articles/779626/).

## License

MIT for the code in this repo (my own work). The referenced vendor firmware/BootROM/asset
images are **not** included and remain their respective owners'.
