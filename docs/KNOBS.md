# Tunable value-knobs (flash-patch via the generic `patch` mode)

Each is a safe, reversible byte patch inside the loaded app. Dry-run first, add `WRITE` to apply, and use
`flashrestore`/re-patch to revert. Offsets are FLASH offsets in your 4 MiB dump. Usage:

```bash
sudo venv/bin/python device/kdc_run.py patch YOUR-4MB-DUMP.bin 0xOFF=0xVAL[,0xOFF=0xVAL...] [WRITE]
```

## Thermal print (printer IS present: 58 mm, 384 dots/line, ink-free thermal)
The print path has TWO tone modes. Confirm which your unit uses by testing.
- **Fixed-threshold mode** — one global threshold at 128 decides black/white per dot (harsh, no midtones).
  - `0x4d440` (RAM 0x0204b040, `l.sfgtui r6,128`): the threshold. **Lower = darker / more highlight detail,
    higher = lighter.** Codex suggests **144 (0x90)** to recover midtones. `patch DUMP 0x4d440=0x90 WRITE`
- **32-plane grayscale mode** — prints each line in 32 passes with ascending thresholds 0,8,16,…,248
  (a linear tone ramp). This is the real grayscale path.
  - `0x4cb5c` (RAM 0x0204a75c, `l.addi r2,r2,8`): the threshold STEP (8). Smaller step = finer/lighter ramp
    scaling; this only RESCALES linearly. A true gamma curve needs code injection (see FINDINGS / stretch).
- DO NOT touch the 0x8000-cap sites (0x0204ab80/88, 0x0204b0c4/cc) or the shared delay/motor timing.

## Remove shipped debug code
- `0x37414` (RAM 0x02035014, `l.jal 0x02030c48`): the call to the BACK_JP* JPEG-register debug dump.
  NOP it (4 bytes) to drop the debug dump from that path:
  `patch DUMP 0x37414=0x00,0x37415=0x00,0x37416=0x00,0x37417=0x15 WRITE`  (0x15000000 = l.nop)

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
