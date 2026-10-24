#!/usr/bin/env python3
"""Offline LOGIC tests for the sensor/print byte-patch commands (awb, print-tone, jpeg-quality).

flsim.py proves the flash WRITE PATH (the SPI payload: erase/program/verify, bounds, fault injection).
This proves the EDITS each command computes produce exactly the intended firmware change, by exercising the
REAL functions from device/kdc_run.py — awb_edits/awb_gate, print_tone_edits, unmanaged_diffs (the retune
guard) — not reimplementations, so a regression in the shipped builders fails the test. The USB backend is
stubbed so importing kdc_run runs no device code.

Run:  KDC_DUMP=/path/to/full_backup_4mb.bin python3 tools/test_patches.py
Without KDC_DUMP the dump-dependent disassembly checks are skipped (and the summary says so).
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
sys.modules['kdc_dump'] = types.ModuleType('kdc_dump')     # kdc_run does `import kdc_dump as K` (used at runtime only)
sys.path.insert(0, os.path.join(HERE, '..', 'device'))
import kdc_run as R
from or1k_dis import dis

BASE = 0x01ffdc00
SCCB = 0x02047a34
dump_path = os.environ.get('KDC_DUMP')
d = open(dump_path, 'rb').read() if dump_path and os.path.exists(dump_path) else None
skipped = 0


def apply(edits):
    b = bytearray(d)
    for o, v in edits.items():
        b[o] = v
    return b


def sccb_seq(b, lo, hi):
    """Reconstruct the (reg,val) sccb_write sequence in [lo,hi) from a patched image."""
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


def test_constants():
    assert len(R.AWB_SITES) == 10, R.AWB_SITES
    nones = [off for off, _ow, imm in R.AWB_SITES if imm is None]
    assert nones == [R.AWB_RGAIN_OFF, R.AWB_BGAIN_OFF] == [0x4b22c, 0x4b244]
    assert all((off & ~0xfff) == R.AWB_SECTOR for off, _, _ in R.AWB_SITES)
    assert R.PRINT_THRESH_OFF == 0x4d440 and R.PRINT_THRESH_WORD == 0xbc460080
    print('  [ok] constants: AWB_SITES shape/sector, gain offsets, print threshold offset+word')


def test_awb_edits_real():
    # exercise the SHIPPED builder; it must cover all 10 sites x 2 bytes = 20 edits, all in-sector
    e = R.awb_edits(0x88, 0xd0)
    assert len(e) == 20 and all((o & ~0xfff) == R.AWB_SECTOR for o in e)
    assert e[R.AWB_RGAIN_OFF] == 0x88 and e[R.AWB_BGAIN_OFF] == 0xd0
    e2 = R.awb_edits(0x70, 0xe0)
    assert e2[R.AWB_RGAIN_OFF] == 0x70 and e2[R.AWB_BGAIN_OFF] == 0xe0    # gains parameterised into the right slots
    assert R.print_tone_edits(0x90) == {0x4d440: 0x90, 0x4d441: 0x00}
    print('  [ok] awb_edits/print_tone_edits (shipped builders): coverage, in-sector, gains in R/B slots')


def test_awb_gate_and_sequence():
    global skipped
    if d is None:
        skipped += 1; print('  [skip] awb gate+sequence (no KDC_DUMP)'); return
    assert R.awb_gate(d) is None, R.awb_gate(d)             # the real gate accepts this exact firmware
    tampered = bytearray(d); tampered[R.AWB_SITES[0][0]] ^= 0xff
    assert R.awb_gate(bytes(tampered)) is not None          # and rejects a changed site
    b = apply(R.awb_edits(0x88, 0xd0))
    seq = sccb_seq(b, 0x02048d80, 0x02048e80)
    assert seq[-5:] == [(0x32, 0x05), (0xfd, 0x02), (0x26, 0x88), (0x27, 0xd0), (0xfd, 0x01)], seq[-5:]
    # operand-only: each changed WORD stays l.addi AND the full word equals orig with just the imm16 swapped
    for off, ow, imm in R.AWB_SITES:
        want_lo = imm if imm is not None else (0x88 if off == R.AWB_RGAIN_OFF else 0xd0)
        got = struct.unpack_from('<I', b, off)[0]
        assert got >> 16 == ow >> 16 and (got >> 26) == 0x27 and (got & 0xffff) == want_lo, f'@0x{off:x} {got:08x}'
    print('  [ok] awb_gate accepts/rejects; patched init emits the exact 5-write AWB sequence; operands-only')


def test_print_tone_real():
    global skipped
    if d is None:
        skipped += 1; print('  [skip] print-tone (no KDC_DUMP)'); return
    assert struct.unpack_from('<I', d, R.PRINT_THRESH_OFF)[0] == R.PRINT_THRESH_WORD
    for th in (0x60, 0x90, 0xb0):
        b = apply(R.print_tone_edits(th))
        assert dis(struct.unpack_from('<I', b, R.PRINT_THRESH_OFF)[0], 0x0204b040) == f'l.sfgtui r6,{th}'
    print('  [ok] print_tone_edits re-encodes the threshold to l.sfgtui r6,<T> for 0x60/0x90/0xb0')


def test_jpeg_quality():
    global skipped
    if d is None:
        skipped += 1; print('  [skip] jpeg-quality (no KDC_DUMP)'); return
    assert list(d[R.PHOTO_QUANT_OFF:R.PHOTO_QUANT_OFF + 8]) == [16, 11, 15, 23, 28, 24, 22, 25]
    print('  [ok] jpeg-quality: photo quant set B present at the expected offset')


def test_managed_guard_real():
    # exercise the SHIPPED retune guard unmanaged_diffs()
    sec = 0x4b000; backup_sec = bytes(0x1000); managed = {sec + 5, sec + 6}
    assert R.unmanaged_diffs(backup_sec, backup_sec, managed, sec) == []          # clean
    live = bytearray(backup_sec); live[5] = 1; live[6] = 2
    assert R.unmanaged_diffs(bytes(live), backup_sec, managed, sec) == []          # only our bytes -> re-patch ok
    live2 = bytearray(backup_sec); live2[10] = 1
    assert R.unmanaged_diffs(bytes(live2), backup_sec, managed, sec) == [sec + 10]  # a foreign byte -> blocked
    # a real awb retune state: live == clean + a PRIOR awb run; guard must allow it with managed=keys
    if d is not None:
        bs = d[R.AWB_SECTOR:R.AWB_SECTOR + 0x1000]
        prior = bytearray(bs)
        for o, v in R.awb_edits(0xaa, 0xbb).items():
            prior[o - R.AWB_SECTOR] = v
        assert R.unmanaged_diffs(bytes(prior), bs, set(R.awb_edits(0x88, 0xd0).keys()), R.AWB_SECTOR) == []
    print('  [ok] unmanaged_diffs (shipped guard): clean / own-bytes re-patch / foreign-byte block')


def test_flash_edits_integration():
    # Drive the REAL flash_edits() offline with fakes. Inject an UNMANAGED change at the pre-erase read and
    # assert it aborts (SystemExit) and never calls _write_sector — i.e. the guard protects the bytes about
    # to be overwritten, not just the first read.
    sec = R.SECTOR
    backup = bytearray(0x10000)                      # clean, all 0x00
    edits = {sec + 5: 0xAA}; managed = set(edits.keys())
    gate_sec = bytes(backup[sec:sec + 0x1000])       # gate read: clean (== backup)
    tampered = bytearray(gate_sec); tampered[10] = 0x99   # pre-erase read: an unmanaged byte changed
    reads = iter([gate_sec, bytes(tampered)])
    writes = []
    orig = (R.enter_loader, R._upload_payload, R._read_range, R._granule_preflight, R._write_sector)
    R.enter_loader = lambda: type('B', (), {'release': lambda self: None})()
    R._upload_payload = lambda bot, window: (lambda *a, **k: (0, R.JEDEC))
    R._read_range = lambda cmd, s, n: next(reads)
    R._granule_preflight = lambda bot: None
    R._write_sector = lambda cmd, s, data: writes.append(s)
    try:
        raised = False
        try:
            R.flash_edits(bytes(backup), edits, 'WRITE', strict=True, managed=managed)
        except SystemExit:
            raised = True
    finally:
        R.enter_loader, R._upload_payload, R._read_range, R._granule_preflight, R._write_sector = orig
    assert raised, 'flash_edits did not abort on an unmanaged change at erase time'
    assert writes == [], f'_write_sector ran despite the abort: {writes}'
    print('  [ok] flash_edits integration: unmanaged change at erase time -> abort, nothing written')


def test_flashpatch_guard():
    global skipped
    if d is None:
        skipped += 1; print('  [skip] flashpatch guard (no KDC_DUMP)'); return
    # the REAL _build_patched with guard=True: both delay immediates -> 250, and the settle sub operand
    # changes r11 -> r24 so r3 becomes a positive constant (underflow-proof); diff stays within MANAGED_GUARD
    patched, _cur = R._build_patched(d, 250, guard=True)
    sec = R.SECTOR
    assert struct.unpack_from('<I', patched, R.PATCH_OFF - sec)[0] & 0xffff == 250
    assert struct.unpack_from('<I', patched, R.PATCH_OFF2 - sec)[0] & 0xffff == 250
    assert struct.unpack_from('<I', patched, 0x9f70 - sec)[0] == R.GUARD_NEW
    assert dis(R.GUARD_NEW, 0x02007b70) == 'l.sub r3,r18,r24'
    diff = {sec + i for i in range(0x1000) if patched[i] != d[sec + i]}
    assert diff <= R.MANAGED_GUARD, sorted(hex(x) for x in diff)
    # without guard, the sub is untouched
    p2, _ = R._build_patched(d, 250, guard=False)
    assert struct.unpack_from('<I', p2, 0x9f70 - sec)[0] == R.GUARD_OLD
    # GUARD with ticks < 6 would make SETTLE-5 <= 0 (re-underflow) and must be refused
    for bad in (1, 5):
        try:
            R._build_patched(d, bad, guard=True); raised = False
        except SystemExit:
            raised = True
        assert raised, f'GUARD with ticks={bad} was not refused'
    print('  [ok] flashpatch GUARD: delays->250 + l.sub r11->r24 (positive constant); ticks<6 refused; diff in MANAGED_GUARD')


def test_flashpatch_erase_guard():
    # Drive the REAL flashpatch() offline; the live sector is clean at the first gate but an UNMANAGED byte
    # changes at the post-preflight re-read. flashpatch must abort (SystemExit) without calling _write_sector.
    global skipped
    if d is None:
        skipped += 1; print('  [skip] flashpatch erase-time guard (no KDC_DUMP)'); return
    sec = R.SECTOR
    orig = d[sec:sec + 0x1000]
    tampered = bytearray(orig); tampered[0x600] ^= 0xff          # a byte well outside the managed delay/guard set
    reads = iter([bytes(orig), bytes(tampered)])                 # gate read (clean) ; post-preflight read (tampered)
    writes = []
    orig_fns = (R.enter_loader, R._upload_payload, R._read_range, R._granule_preflight, R._write_sector)
    R.enter_loader = lambda: type('B', (), {'release': lambda self: None})()
    R._upload_payload = lambda bot, window: (lambda *a, **k: (0, R.JEDEC))
    R._read_range = lambda cmd, s, n: next(reads)
    R._granule_preflight = lambda bot: None
    R._write_sector = lambda cmd, s, data: writes.append(s)
    try:
        raised = False
        try:
            R.flashpatch(dump_path, 250, 'WRITE', guard=False)
        except SystemExit:
            raised = True
    finally:
        R.enter_loader, R._upload_payload, R._read_range, R._granule_preflight, R._write_sector = orig_fns
    assert raised, 'flashpatch did not abort on an unmanaged change at erase time'
    assert writes == [], f'_write_sector ran despite the abort: {writes}'
    print('  [ok] flashpatch erase-time guard: unmanaged change after preflight -> abort, nothing written')


def test_wrappers_pass_managed():
    global skipped
    if d is None:
        skipped += 1; print('  [skip] wrapper strict/managed capture (no KDC_DUMP)'); return
    cap = {}
    orig = R.flash_edits
    R.flash_edits = lambda backup, edits, confirm, strict=True, managed=None: cap.update(
        strict=strict, managed=managed, keys=set(edits.keys()))
    try:
        R.awb(dump_path, 0x88, 0xd0, 'DRY')
        assert cap['strict'] is True and cap['managed'] == cap['keys'] == set(R.awb_edits(0x88, 0xd0).keys())
        R.print_tone(dump_path, 0x90, 'DRY')
        assert cap['strict'] is True and cap['managed'] == cap['keys'] == set(R.print_tone_edits(0x90).keys())
    finally:
        R.flash_edits = orig
    print('  [ok] awb()/print_tone() call flash_edits with strict=True and managed=set(edits.keys())')


if __name__ == '__main__':
    print(f'patch logic tests ({"real dump" if d else "NO KDC_DUMP — dump checks will be skipped"}):')
    test_constants()
    test_awb_edits_real()
    test_awb_gate_and_sequence()
    test_print_tone_real()
    test_jpeg_quality()
    test_managed_guard_real()
    test_flash_edits_integration()
    test_flashpatch_guard()
    test_flashpatch_erase_guard()
    test_wrappers_pass_managed()
    if skipped:
        print(f'  PASSED (constants/builder checks) — {skipped} dump-dependent group(s) SKIPPED (set KDC_DUMP for full)')
    else:
        print('  ALL PATCH LOGIC TESTS PASSED (incl. dump-backed sequence/threshold checks)')
