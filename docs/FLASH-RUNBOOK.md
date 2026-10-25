# Flash runbook — on-device test session (do this at the bench)

Everything below was prepared and offline-validated (flsim + tools/test_patches.py green, Codex-reviewed:
"no remaining offline defect"). The one thing that could NOT be done without the camera is the actual
flash + visual check — that is this session. Work through it in order; each step is a dry-run first, then
`WRITE`, and every change is a single-sector operand/data patch reversible with `flashrestore`.

Prereqs: `cd` to the repo, `sudo venv/bin/python …` (USB needs sudo on macOS). If a tool says the camera is
not in stock mode, power-cycle it and retry.

## 0. Fresh full backup FIRST (non-negotiable — this is your only recovery source)
```bash
sudo venv/bin/python device/kdc_run.py dump 0 0x400000 full_backup_4mb.bin
```
Keep this file safe. Every patch below reads it; every revert restores from it. Use THIS unit's own backup.

## 1. Apply the SAFE, high-confidence patches first (verify the camera still boots + shoots after each)
Dry-run (no `WRITE`) prints the diff and does JEDEC+read only; add `WRITE` to commit. Power-cycle + take a
photo/boot after each before moving on.
```bash
# shutter lag 1000->250 ticks (+ optional underflow GUARD; GUARD needs ticks>=6)
sudo venv/bin/python device/kdc_run.py flashpatch full_backup_4mb.bin 250 GUARD        # dry-run
sudo venv/bin/python device/kdc_run.py flashpatch full_backup_4mb.bin 250 GUARD WRITE  # commit
# silence power on/off jingles
sudo venv/bin/python device/kdc_run.py silence-sounds full_backup_4mb.bin WRITE
# photo JPEG quality ~Q45 -> Q90
sudo venv/bin/python device/kdc_run.py jpeg-quality full_backup_4mb.bin 90 WRITE
```

## 2. The two EXPERIMENTAL, tune-on-device patches (confirm direction on the first shot/print, then iterate)
```bash
# White balance (+ optional brightness / low-light). Apply INCREMENTALLY and shoot after each (revert any
# with `flashrestore full_backup_4mb.bin 0x4b000 WRITE`), because each extra number sacrifices one more
# page-1 init write (R B = AWB only; +AE adds 0xcb brightness; +MAXGAIN adds the 0x04 gain ceiling = 7 total).
sudo venv/bin/python device/kdc_run.py awb full_backup_4mb.bin 0x88 0xd0              # 1) WB only, dry-run
sudo venv/bin/python device/kdc_run.py awb full_backup_4mb.bin 0x88 0xd0 WRITE        #    commit, shoot indoors
sudo venv/bin/python device/kdc_run.py awb full_backup_4mb.bin 0x88 0xd0 0x48 WRITE   # 2) +brightness (AE target; 0x45 dim..0x50 bright)
sudo venv/bin/python device/kdc_run.py awb full_backup_4mb.bin 0x88 0xd0 0x48 0xa0 WRITE  # 3) +low-light gain cap (lower=cleaner/darker)
#   too warm -> lower R (0x78) / raise B (0xe0). Always re-run with the ORIGINAL dump (it retunes from it).
#   Omit a trailing number to drop that knob — but if you already flashed it, flashrestore 0x4b000 first.
#   WB is now FIXED (auto-AWB off): great indoors, may look cool in daylight. After a resolution/mode
#   change, recheck WB (a RAM reg-table replay may re-touch the sensor).

# Print darkness (fixed-threshold mode): higher = darker. Default 0x90 (slightly darker than stock 128).
sudo venv/bin/python device/kdc_run.py print-tone full_backup_4mb.bin 0x90 WRITE    # then print a photo
#   too faint -> raise toward 0xb0; too dark -> lower toward 0x70. If your unit prints in 32-plane
#   grayscale instead (shaded, not 1-bit), tune the ramp step: patch full_backup_4mb.bin 0x4cb5c=0x06 WRITE
```

## 3. If anything looks wrong — revert just that patch (device still boots into the loader)
```bash
sudo venv/bin/python device/kdc_run.py flashrestore full_backup_4mb.bin 0x9000 WRITE   # shutter+guard
sudo venv/bin/python device/kdc_run.py flashrestore full_backup_4mb.bin 0x4b000 WRITE  # AWB
sudo venv/bin/python device/kdc_run.py flashrestore full_backup_4mb.bin 0x4d000 WRITE  # print-tone
sudo venv/bin/python device/kdc_run.py flashrestore full_backup_4mb.bin 0x6d000 WRITE  # jpeg-quality
```
`flashrestore` has no equality gate (it is the recovery path) — only ever run it with THIS unit's own
backup. Worst case for any patch is a bad image/sensor glitch that a power-cycle clears; only corrupting
sector 0 would need the hardware short recovery (none of these patches touch it).

## What to report back for the next tuning round
- Shutter: lag feel OK at 250, exposure still good?
- WB: still orange / now too cool / neutral? (so we pick R/B)
- Print: too faint / too dark / which tone mode your unit uses
- Anything that failed to boot or shoot after a specific step (name the step).
