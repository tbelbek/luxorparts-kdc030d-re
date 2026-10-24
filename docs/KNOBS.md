# Tunable value-knobs (flash-patch via the generic `patch` mode)

Each is a safe, reversible byte patch inside the loaded app. Dry-run first, add `WRITE` to apply, and use
`flashrestore`/re-patch to revert. Offsets are FLASH offsets in your 4 MiB dump. Usage:

```bash
sudo venv/bin/python device/kdc_run.py patch YOUR-4MB-DUMP.bin 0xOFF=0xVAL[,0xOFF=0xVAL...] [WRITE]
```

## Thermal print tone (printer IS present: 58 mm, 384 dots/line, ink-free thermal)
The print path has TWO tone modes. Confirm which your unit uses by testing (print a photo and see whether it
is 1-bit harsh or has grey shading).
- **Fixed-threshold mode** — one global threshold decides black/white per dot. A pixel prints BLACK when
  `pixel <= threshold`, so **higher threshold = more black = DARKER; lower = lighter.** Dedicated command:
  ```bash
  sudo venv/bin/python device/kdc_run.py print-tone YOUR-4MB-DUMP.bin [THRESHOLD] WRITE   # default 0x90
  ```
  Default 0x90 (144, slightly darker than stock 128) recovers faint midtones; raise toward 0xb0 for a darker
  print, lower toward 0x60 for lighter. Re-tunable (rewrites from the clean backup). Revert:
  `flashrestore DUMP 0x4d000 WRITE`. (Offset 0x4d440, `l.sfgtui r6,128` — operand-only, safe tier.)
- **32-plane grayscale mode** — prints each line in 32 passes with ascending thresholds 0,8,16,…,248 (a
  linear tone ramp built on the stack). The real grayscale path, but the ramp is COMPUTED, not a table.
  - `0x4cb5c` (RAM 0x0204a75c, `l.addi r2,r2,8`): the ramp STEP (8). Smaller step = the ramp tops out sooner
    (compresses toward lighter); this only RESCALES LINEARLY. `patch DUMP 0x4cb5c=0x06 WRITE` for a trial.
  - A true GAMMA curve or error-diffusion DITHER (the ideal for photos on a 1-bit head) needs a non-linear
    LUT = added code; this packed image has no safe home for injection (see FINDINGS.md), so it is NOT
    flashable — the threshold/step knobs are the safe, flashable reach.
- DO NOT touch the 0x8000-cap sites (0x0204ab80/88, 0x0204b0c4/cc) or the shared delay/motor timing.

## Shipped debug code — nothing to remove (and do NOT NOP 0x37414)
The firmware carries 45 `BACK_JPCON1=%X`-style debug format strings (flash 0x6d3f4+) and a function that
printf's the JP registers (0x02030d3c–0x020311b0). A static search finds **no reference** to that function
(no call/branch/pointer/address-build), so it is very likely leftover debug that never runs — though a
static search can't fully prove unreachability (an indirect call could evade it). Either way there is no
safe, worthwhile patch to "remove" it (you cannot reclaim the space for anything executable on this packed
image), and the prints go to a serial sink that is invisible on a production unit. An earlier draft of this file told you to NOP `0x37414` as "the debug dump call" — that was WRONG:
0x37414 calls 0x02030c48, which **writes JPEG hardware SPRs (0xb18c–0xb1b0) from a config struct**. NOPing
it would drop essential codec configuration and corrupt JPEG. Do not patch it. (Details in FINDINGS.md.)

## White balance — fix the warm/orange indoor cast (`awb` command, NOT a `patch` knob)
The SP0A39 runs factory auto-AWB (firmware never configures it). `awb` repurposes the 5 trailing page-1
sensor-init writes IN PLACE (l.addi immediate operand bytes only — same reversible, boots-to-loader recovery
tier as the shutter patch) into: page1 `0x32=0x05` (auto-AWB OFF) → page2 `0x26`=R-gain, `0x27`=B-gain →
page1 restore. `0x80`=1.0x; **lower R / higher B cools the orange.**
```bash
sudo venv/bin/python device/kdc_run.py awb YOUR-4MB-DUMP.bin [RGAIN BGAIN] WRITE   # defaults 0x88 0xd0
```
- Dry-run first (omit `WRITE`). Retune by re-running with new R/B (it rewrites the sector from the clean
  backup each time — always pass the ORIGINAL dump). Full revert: `flashrestore DUMP 0x4b000 WRITE`.
- Starting points (SP0A20 sibling presets, 0x80=1.0x): incandescent `0x88 0xd0` (default, strongest cool),
  fluorescent `0xbf 0xcc`, neutral `0xac 0xad`. Tune on device.
- WATCH: auto-AWB is now OFF so WB is FIXED — great indoors, may look cool in daylight. After a
  resolution/mode change the firmware replays a RAM sensor table that could re-touch WB; recheck.

## Revert any single patch
`flashrestore DUMP [SECTOR] WRITE` rewrites one 4 KiB sector from the backup. Default sector `0x9000`
(shutter); pass `0x4b000` (AWB), `0x6d000` (jpeg-quality), or a print-knob sector to revert that patch.

## Already applied earlier (for reference)
- Shutter lag: `flashpatch DUMP 250 WRITE` (both delay immediates 0x9f48/0x9f64).
- Power sounds: `silence-sounds DUMP WRITE`.
- Photo JPEG quality: `jpeg-quality DUMP 90 WRITE` (quant set B @0x6d1a4).
