# AGENTS.md — orientation for agents & contributors

This repo reverse-engineers and patches a **Luxorparts KDC-030D** kids' camera over its
stock USB port. Read this before touching the firmware or the tools. The README has the
user-facing story; this file is the working brief.

## Hardware facts (memorise these)

- **SoC:** AppoTech / Buildwin **JT5295** (AX3295B family). CPU is **OpenRISC 1000 (ORBIS32),
  little-endian.**
- **Flash:** **Zbit ZB25VQ32**, 4 MiB SPI NOR, JEDEC **`5e 40 16`**. Erase/program geometry
  (standard, device-wide): **4 KiB sector erase = `0x20`**, 32 KiB = `0x52`, 64 KiB = `0xd8`,
  **page program = `0x02`** (≤256 B, no page-boundary crossing), read = `0x03`, RDSR = `0x05`
  (WIP=bit0, WEL=bit1), WREN = `0x06`. Program only clears 1→0 bits; erase a sector first.
- **BootROM:** 32 KiB mask ROM @ `0x00100000`, byte-identical to penguin-camera's. It reads the
  flash header, copies the app to DDR, and jumps. **It does NOT checksum the app** (so a 2-byte
  patch needs no checksum fix-up — confirmed: the SDK installs per-sector).
- **USB identities:** stock `0219:3280`→`1908:3283`; loader (after `0xDA`) `1908:3319` "BLDR v1.00".

## ISA quirks that WILL bite you

- **No branch delay slot, but a taken branch SKIPS the next instruction.** Every `l.bf/l.bnf/
  l.j/l.jal/l.jr` in hand-written payloads is followed by an `l.nop`. Omitting it changes which
  instruction runs.
- **`l.jal` links r9 = PC+4** (the instruction right after the call), i.e. the "delay-slot"
  instruction executes *after* the call returns. `tools/flstub.py` relies on this.
- **Immediates sign-extend.** `l.sfgeui r,imm` with `imm ≥ 0x8000` sign-extends to `0xFFFF8000+`,
  so window bounds like `0x9000` are loaded into a register and compared with `l.sfgeu` instead.
- **Callee-saved registers must be preserved** (`r14/r16/r18/r20`). The earliest payloads hung
  the BootROM loader precisely because they clobbered these without saving — six dead ends.
- Hand-assembled payloads are built by `tools/stub_build.py`'s `Asm` (emits LE words directly);
  there is no OpenRISC toolchain in the loop (bootlin's or1k gcc is big-endian only).

## The workflow

1. **Back up first, always:** `dump 0 0x400000 full_backup_4mb.bin` (read-only, ~a few min).
2. Disassemble: `tools/or1k_dis.py FILE BASE START END` — START/END are **file offsets**, BASE
   is the display base. For the app use BASE `0x01ffdc00` (so `addr = file_offset + 0x01ffdc00`).
3. Locate the code to change; build/adjust the patch in `device/kdc_run.py`.
4. **Validate offline:** `tools/flsim.py` (needs `KDC_PENGUIN_REPO`; dump optional). It must stay
   green: full write flow, bounds rejection, fault-injection, exact 2-byte diff.
5. **Flash with the `WRITE` gate** and let the host's preflight + verify run. Never skip the
   backup or the dry run.

## Rules that keep writes safe (do not weaken)

- The write payload is **window-restricted at build time** (`flstub.build(window=(lo,hi))`); it
  can only erase/program inside that window. Patch sector → `(0x9000,0xa000)`.
- `flashpatch`/`flashrestore` do **nothing destructive without the literal final arg `WRITE`**.
  A dry run is JEDEC + read only.
- Before erasing `0x9000`, the host runs an **erase-granule preflight** in blank flash at
  `0x200000` to prove `0x20` erases exactly 4 KiB (it shares a 64 KiB block with the boot
  sector — a 64 KiB erase there would brick boot). The scratch block is blank, so the test is
  zero-risk; it refuses to run if that block is not blank.
- Every write verifies: live==backup gate → post-erase all-`0xFF` → post-program byte-exact.
- **Recovery needs no case opening:** the shutter patch is off the boot/USB path, so a bad value
  still boots and re-enters the `0xDA` loader; `flashrestore` rewrites the sector from the backup.
  Only corrupting sector 0 / the bootstrap would require the hardware short recovery.

## Known findings

- Shutter lag = fixed ~1000-tick settle at flash **`0x9f48`** (RAM `0x02007b48`), word
  `0x9e5803e8` = `l.addi r18,r24,1000`. Lowering the `1000` immediate shrinks the lag. 250 was
  flashed and verified on hardware (lag ~1 s → ~¼ s, exposure fine); tune via the tool.
- Assets live at flash `0x7ea00..0x1ce000`: JPEG UI graphics, PCM (WAV) sounds, a bitmap font
  (no TTF/OTF), indexed by a `SFAT` table (header +0x304). Carve by signature from a dump.
- Power-on/off jingles = the two longest WAVs (0x145c22, 0x15409a). The player ignores the WAV
  header length, so zeroing the RIFF/data size does NOT silence them; zeroing the PCM SAMPLES does
  (`silence-sounds` mode). Flashed + verified on hardware.
- `tools/xref.py` gives who-calls-what over an or1k_dis listing; use it instead of Ghidra/radare2,
  whose stock OpenRISC model mis-handles this SoC's no-delay-slot calls.

## Env / setup

```
KDC_LIBUSB         path to libusb (pyusb backend); default /opt/homebrew/lib/libusb-1.0.dylib
KDC_PENGUIN_REPO   path to a penguin-camera checkout (tools/flsim.py uses its or1k_subset.py)
KDC_DUMP           optional path to your 4 MiB dump (tools/flsim.py uses it instead of a synthetic image)
```
macOS: USB commands need `sudo` (libusb detaches the mass-storage driver). The camera returns
to stock USB mode after each loader run; power-cycle if a tool says it is not in stock mode.

## Do NOT commit

Vendor firmware, BootROM dumps, or extracted assets (JPEG/WAV/font) — they are not ours to
redistribute. `.gitignore` already excludes `*.bin` (except the small probe stubs) and `*.log`.
