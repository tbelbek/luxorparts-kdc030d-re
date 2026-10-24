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

## Already applied earlier (for reference)
- Shutter lag: `flashpatch DUMP 250 WRITE` (both delay immediates 0x9f48/0x9f64).
- Power sounds: `silence-sounds DUMP WRITE`.
- Photo JPEG quality: `jpeg-quality DUMP 90 WRITE` (quant set B @0x6d1a4).
