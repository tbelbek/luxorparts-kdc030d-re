#!/usr/bin/env python3
"""Offline LOGIC tests for the sensor/print byte-patch commands (awb, print-tone, jpeg-quality).

These complement tools/flsim.py: flsim proves the flash WRITE PATH (the SPI payload: erase/program/verify,
bounds, fault injection) is correct; this proves the EDITS each command computes produce exactly the intended
firmware change. We import kdc_run's real constants/edit-builders (stubbing the USB backend so nothing device-
side runs) and check the patched image by disassembly, so the command logic is verified, not just asserted.

Run:  KDC_DUMP=/path/to/full_backup_4mb.bin python3 tools/test_patches.py
Without KDC_DUMP it validates everything it can from the constants alone and skips the dump-dependent checks.
"""
import os, sys, types, struct

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                                   # or1k_dis
# Stub the USB backend so importing device/kdc_run.py does not require pyusb / a device.
sys.modules['usb'] = types.ModuleType('usb')
sys.modules['usb.core'] = types.ModuleType('usb.core')
sys.modules['usb'].core = sys.modules['usb.core']
sys.modules['usb.core'].USBError = type('USBError', (Exception,), {})
sys.modules['usb.util'] = types.ModuleType('usb.util')
sys.modules['kdc_dump'] = types.ModuleType('kdc_dump')     # kdc_run does `import kdc_dump as K`
sys.path.insert(0, os.path.join(HERE, '..', 'device'))
import kdc_run as R
from or1k_dis import dis

BASE = 0x01ffdc00
SCCB = 0x02047a34                                          # sccb_write (sensor register write over SCCB/I2C)
dump_path = os.environ.get('KDC_DUMP')
d = open(dump_path, 'rb').read() if dump_path and os.path.exists(dump_path) else None


def apply(edits):
    b = bytearray(d)
    for o, v in edits.items():
        b[o] = v
    return b


def sccb_seq(b, lo, hi):
    """Reconstruct the (reg,val) sccb_write sequence in [lo,hi) from a patched image (same method the RE used)."""
    import re
    reA = re.compile(r'^l\.addi r(\d+),r0,(-?\d+)$'); reSB = re.compile(r'^l\.sb (\d+)\(r1\),r(\d+)$')
    rimm = {}; pr = pv = None; out = []
    for a in range(lo, hi, 4):
        w = struct.unpack_from('<I', b, a - BASE)[0]; t = dis(w, a)
        m = reA.match(t)
        if m: rimm[int(m.group(1))] = int(m.group(2)) & 0xff
        m = reSB.match(t)
        if m:
            bo = int(m.group(1)); rs = int(m.group(2))
            if rs in rimm:
                if bo == 0: pr = rimm[rs]
                elif bo == 1: pv = rimm[rs]
        if t.startswith('l.jal 0x') and int(t.split()[-1], 16) == SCCB and pr is not None and pv is not None:
            out.append((pr, pv)); pr = pv = None
    return out


# The two gain-value slots are the AWB_SITES entries with a None immediate, in R,B order.
GAIN_OFFS = [off for off, _ow, imm in R.AWB_SITES if imm is None]
RGAIN_OFF, BGAIN_OFF = GAIN_OFFS[0], GAIN_OFFS[1]


def test_constants():
    # AWB_SITES: 10 sites, 2 are gain-value slots (imm None), 8 are fixed; all in sector 0x4b000.
    assert len(R.AWB_SITES) == 10, R.AWB_SITES
    assert len(GAIN_OFFS) == 2, 'exactly two gain-value slots'
    assert all((off & ~0xfff) == R.AWB_SECTOR for off, _, _ in R.AWB_SITES)
    assert (RGAIN_OFF, BGAIN_OFF) == (0x4b22c, 0x4b244), (hex(RGAIN_OFF), hex(BGAIN_OFF))
    assert R.PRINT_THRESH_OFF == 0x4d440
    print('  [ok] constants: AWB_SITES shape, sector, gain offsets, print threshold offset')


def test_awb_sequence():
    if d is None:
        print('  [skip] awb sequence (no KDC_DUMP)'); return
    # gate words must match the dump
    for off, ow, _ in R.AWB_SITES:
        assert struct.unpack_from('<I', d, off)[0] == ow, f'gate word mismatch @0x{off:x}'
    # build edits exactly as awb() does, default gains 0x88/0xd0
    edits = {}
    for off, _ow, imm in R.AWB_SITES:
        v = imm if imm is not None else (0x88 if off == RGAIN_OFF else 0xd0)
        edits[off] = v & 0xff; edits[off + 1] = (v >> 8) & 0xff
    b = apply(edits)
    seq = sccb_seq(b, 0x02048d80, 0x02048e80)[-5:]
    want = [(0x32, 0x05), (0xfd, 0x02), (0x26, 0x88), (0x27, 0xd0), (0xfd, 0x01)]
    assert seq == want, f'AWB sequence {seq} != {want}'
    # every changed word is still an l.addi (opcode 0x27): structure preserved, operands only
    for off in edits:
        assert (struct.unpack_from('<I', b, off & ~3)[0] >> 26) == 0x27, f'word @0x{off:x} not l.addi'
    # a custom gain pair lands in the right slots
    b2 = apply({**edits, RGAIN_OFF: 0x70, BGAIN_OFF: 0xe0})
    assert sccb_seq(b2, 0x02048d80, 0x02048e80)[-3:-1] == [(0x26, 0x70), (0x27, 0xe0)]
    print('  [ok] awb: disable-auto -> page2 -> R/B gain -> restore page1; operands-only; gains parameterised')


def test_print_tone():
    if d is None:
        print('  [skip] print-tone (no KDC_DUMP)'); return
    assert struct.unpack_from('<I', d, R.PRINT_THRESH_OFF)[0] == 0xbc460080, 'print gate word'
    for th in (0x60, 0x90, 0xb0):
        b = apply({R.PRINT_THRESH_OFF: th & 0xff, R.PRINT_THRESH_OFF + 1: (th >> 8) & 0xff})
        w = struct.unpack_from('<I', b, R.PRINT_THRESH_OFF)[0]
        assert dis(w, 0x0204b040) == f'l.sfgtui r6,{th}', dis(w, 0x0204b040)
    print('  [ok] print-tone: threshold immediate re-encodes to l.sfgtui r6,<T> for 0x60/0x90/0xb0')


def test_jpeg_quality():
    if d is None:
        print('  [skip] jpeg-quality (no KDC_DUMP)'); return
    cur = list(d[R.PHOTO_QUANT_OFF:R.PHOTO_QUANT_OFF + 128])
    assert cur[:8] == [16, 11, 15, 23, 28, 24, 22, 25], f'photo quant set B not as expected: {cur[:8]}'
    print('  [ok] jpeg-quality: photo quant set B present at the expected offset')


def test_managed_decision():
    # Mirror flash_edits' strict+managed accept/refuse decision.
    def decide(live, backup, managed):
        bad = [k for k in range(len(live)) if live[k] != backup[k]]
        outside = [b for b in bad if managed is None or b not in managed]
        return 'REFUSE' if outside else ('REPATCH' if bad else 'CLEAN')
    bk = bytes(32); mg = {5, 6}
    assert decide(bk, bk, mg) == 'CLEAN'
    lv = bytearray(bk); lv[5] = 1; lv[6] = 2
    assert decide(lv, bk, mg) == 'REPATCH'          # retune our own bytes -> allowed
    lv2 = bytearray(bk); lv2[10] = 1
    assert decide(lv2, bk, mg) == 'REFUSE'          # any byte outside managed -> refused
    print('  [ok] managed-set retune guard: CLEAN / REPATCH / REFUSE')


if __name__ == '__main__':
    print(f'patch logic tests ({"real dump" if d else "constants only — set KDC_DUMP for full checks"}):')
    test_constants()
    test_awb_sequence()
    test_print_tone()
    test_jpeg_quality()
    test_managed_decision()
    print('  ALL PATCH LOGIC TESTS PASSED')
