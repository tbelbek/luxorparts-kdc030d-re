# KDC-030D firmware — RE findings & review log

Detailed reverse-engineering notes behind the tooling. The README is the how-to; this is the
"what we learned". All offline work is from a full 4 MiB dump of my own unit.

## Chip / layout
- SoC AppoTech/Buildwin **JT5295** (AX3295B), **OpenRISC-1000 little-endian**, non-standard:
  no branch delay slot, a taken branch skips the next instruction, `l.jal` links r9 = PC+4.
- Flash **Zbit ZB25VQ32**, 4 MiB, JEDEC `5e4016`; 4 KiB sector erase `0x20` (verified on-device via
  the granule preflight), 256 B page program `0x02`.
- App CODE: flash `0x2400..0x7ea00` → RAM `0x02000000` (995 sectors, one contiguous block; `RAM =
  flash + 0x01ffdc00`). DATA/assets `0x7ea00..0x1ce000`; rest blank.
- BootROM (mask) does NOT checksum the app → small byte patches boot fine (SDK installs per-sector).

## Shutter lag (fixed)
- Capture function entry **0x020078cc**. Before the frame grab at **0x02007164** it enforces a
  minimum elapsed time since an early timestamp `r24 = uptime()`:
  - `0x02007b48` `l.addi r18,r24,1000` → `deadline = r24 + 1000` (main settle)
  - `0x02007b64` `l.addi r3,r0,1000` → the `delay(1000)` arg on the "setup finished in <5 ticks" fast path
- tick ≈ 1 ms, so ~1 s. Both immediates patched 1000 → **250** (flash 0x9f48 + 0x9f64). Flashed and
  verified on hardware; exposure stayed good. Tunable via `flashpatch <ticks>`.
- The loop body only calls uptime/delay (no sensor/DMA), so it is a pure settle (AE-convergence margin),
  which matches the lag being constant across lighting.

## Power-on/off sounds (silenced)
- The two longest WAVs (0x145c22 ~1.83 s, 0x15409a ~1.62 s) are the jingles. **The player ignores the
  WAV header length** — zeroing the RIFF/data size did nothing. Zeroing the PCM SAMPLES to 0x00 works
  (`silence-sounds`); headers + the short button/shutter sounds are kept. 28 data sectors, verified.

## Firmware-wide delay audit
- 54 fixed `delay()` call sites. Call-graph reachability (`tools/xref.py`) from the capture entry
  (0x020078cc → 301 functions) shows the **only** fixed delay on the photo-capture path is the settle
  above. The other ~53 are boot / SD / display / audio settle times (off the photo path) — necessary
  hardware timing, NOT safe to shorten.

## Known issues / rushed markers (candidates to "idealize" — NOT yet changed)
- **Unsigned underflow** in the settle: the deadline is checked at 0x02007b4c–54 but the subtraction
  sample is taken later at 0x02007b6c; if time crosses the deadline between them, `deadline - now`
  underflows to a huge unsigned `delay()` argument. Pre-existing; a bounds check would harden it.
- **Non-wrap-safe timestamps**: absolute unsigned `uptime()` comparisons (0x020079e4, 0x02007ae0,
  0x02007b50) misbehave at the ~49.7-day 32-bit-ms rollover. Not practical for a camera, but sloppy.
- **No explicit AE/exposure-valid gate** before the grab — capture is purely time-gated. Reducing the
  settle trades latency for convergence headroom (worked at 250 ms here, but there is no feedback check).
- Many duplicated/hard-coded settle constants (delay(2000)/delay(1000)/…) scattered across subsystems.

## Tooling (this repo)
- `device/kdc_run.py`: dump / flashpatch (both delay immediates) / flashrestore / silence-sounds.
- `tools/flstub.py`: window-restricted LE SPI payload (cmd 0/1/2/3). `tools/flsim.py`: offline validator
  (runs on a synthetic image with no dump). `tools/or1k_dis.py` + `tools/xref.py`: disassembler + call graph.
- Safety: full backup first; granule preflight; window-guarded payload; readback verify; recovery off the
  boot path (bad shutter/sound value still boots → 0xDA loader → flashrestore).

## Cameras / sensor (identified)
- **Two cameras (front + back), both SuperPix SP0A39** VGA CMOS. Confirmed via Espressif's open driver:
  `SP0A39_SCCB_ADDR 0x21`, `SP0A39_PID 0x0a39` — firmware checks the low ID byte `0x39` (at 0x020489d8).
  Native 640x480; all larger photo sizes (incl. 9280x5160) are upscales. Integrated auto-AWB.
- Sensor registers are written 8-bit over SCCB via `sccb_write` (RAM 0x02047a34); the init is INLINE
  (137 writes, main func 0x02047d44), pages selected by reg 0xfd. A reliable reg/val scan shows the
  firmware selects pages **0, 1, 0x1d, 0x20 — NEVER page 2 — and never writes reg 0x32**, so the SP0A39
  runs its factory auto-AWB. The warm/orange cast under indoor light is that default AWB.

### AWB fix — delivered (`awb` mode), confirmed map
- Register map confirmed on SP0A39 + sibling SP0A20: page-1 **0x32 = AWB control** (0x15 auto / 0x05
  manual); page-2 **0x26 = R gain, 0x27 = B gain** (0x80 = 1.0x). The auto-AWB loop runs continuously and
  overwrites manual gains unless you set 0x32=0x05 FIRST. Lower R / higher B cools the orange.
- Could NOT inject code: the app image is 100% packed (ZERO 0xFF runs in flash 0x2400..0x7ea00) and every
  >=256 B 0x00 gap is live data (e.g. 0x6e538 is referenced), and the init is a 102-branch-target dispatch
  with no straight-line run to compact. Code/hook injection has no safe home here (confirmed with Codex).
- Instead: **repurpose the 5 trailing page-1 init writes IN PLACE** — only l.addi immediate operand bytes
  change (opcodes/flow/count identical), so it is the same brick-safe, reversible tier as the shutter patch,
  NOT a code injection. The patched init emits: `0x32=0x05` (manual) -> `0xfd=0x02` -> `0x26=R` -> `0x27=B`
  -> `0xfd=0x01` (restore the original page-1 exit state). Offline-validated (exact sequence; every changed
  word still l.addi; all in sector 0x4b000). Sacrifices the 5 original trailing fine-tune writes
  (0x69/0x68/0x67/0xb4/0x56), reversible via `flashrestore DUMP 0x4b000 WRITE`.
- Run `awb DUMP [RGAIN BGAIN] WRITE` (defaults 0x88/0xd0, ~incandescent). WATCH on device: WB is now FIXED
  (good indoors, may be too cool in daylight); a resolution/mode change replays a RAM reg-table (writer
  @0x02049338) that could re-touch the sensor — recheck WB after switching modes. Post-init writer #2
  (@0x02049538) writes reg 0x31 (not 0x32/0x26/0x27) so it does not undo the override.

## Thermal printer IS present (correction)
An earlier note here wrongly concluded "no printer" (it had latched onto Photoshop `8BIM`/`printOutput`
metadata in a JPEG asset and an MP3 "head" string). The unit DOES have the built-in ink-free thermal
printer (Kjell product page: "makes black-and-white prints without ink, using thermal paper"). The print
path was then found by hardware pattern: 58 mm head, **384 dots/line = 48 bytes**, with TWO tone modes —
a fixed global threshold (128) and a 32-plane grayscale halftone ramp (thresholds 0,8,…,248). Head bit-bang
via SPR 0x800c (data bit 0x8000, clock 0x2000), strobe via 0x8004. Tunable value-knobs for both tone modes
are in `docs/KNOBS.md` (threshold 0x4d440, ramp step 0x4cb5c). A true gamma/dither curve would need code
injection, which (as the AWB section shows) has no safe home in this packed image — the linear knobs are
the safe reach; the gamma/dither version is documented but NOT flashed.

## JPEG photo quality
- Still photos use quant **set B** (RAM 0x0206ada4 / flash 0x6d1a4), ~Q45. Confirmed by a real photo's DQT.
- `jpeg-quality` rewrites set B's 128-byte luma+chroma to an Annex-K table scaled to a chosen quality (Q90
  flashed). Loader `jpeg_load_tables` @0x0202c788 copies it (zigzag via ZZ @0x0206ae24) into codec SRAM.
- `docs/key-functions.c` holds hand-decompiled, Codex-verified C for uptime/delay/the shutter-lag gate/
  sccb_write/jpeg_load_tables.
