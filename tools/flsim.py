#!/usr/bin/env python3
"""Offline validation of re/flasher.bin (the hand-ported flasher payload): run it on a
modeled OpenRISC CPU + SPI controller + fake flash + request block, and check it
TERMINATES and performs a correct flash read (right TX bytes, right data, status 0).
Catches the infinite-loop/fault bugs that broke earlier hand-assembled stubs — offline."""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))          # for 'flstub'
_PENG = os.environ.get('KDC_PENGUIN_REPO')
if _PENG: sys.path.insert(0, os.path.join(_PENG, 'tools'))               # penguin-camera or1k_subset
from or1k_subset import CPU, MASK, signed

import flstub                                                           # build the payload in-memory
STOP = 0xfffffffc
REQ = 0x3400
ALLOWED = {0x03, 0x9f, 0x05, 0x06, 0x20, 0x02}   # READ, JEDEC, RDSR, WREN, SECTOR-ERASE, PAGE-PROGRAM


class Flash:
    """Models a 64 KiB window of the SPI NOR with the destructive opcodes. Every opcode is
    checked against ALLOWED; erase/program require a prior WREN (WEL) and stay inside their
    granule; program can only clear bits. self.ops logs every destructive op for assertions."""
    def __init__(self, data):
        self.mem = bytearray(data)                       # mutable, len >= 0x10000
        self.cs = 1; self.st = 'idle'; self.cmd = 0; self.addr = 0; self.al = 0; self.rx = 0
        self.tx = []; self.ops = []; self.wel = 0; self.wip = 0
        self._pend = None; self._pbuf = bytearray()
    def csset(self, low):
        if low and self.cs == 1: self.st = 'cmd'
        if (not low) and self.cs == 0: self._commit()    # CS deassert commits a pending op
        self.cs = 0 if low else 1
    def _commit(self):
        if self._pend is None: return
        kind, a = self._pend
        if kind == 'erase':
            assert a & 0xfff == 0, f'erase addr not 4K-aligned: {a:#x}'
            assert self.wel, 'erase without WEL'
            self.mem[a:a + 0x1000] = b'\xff' * 0x1000
            self.ops.append(('erase', a, 0))
        else:  # program
            buf = bytes(self._pbuf)
            assert self.wel, 'program without WEL'
            assert buf and len(buf) <= 256 and a // 256 == (a + len(buf) - 1) // 256, f'bad program {a:#x}+{len(buf)}'
            for i, bb in enumerate(buf):
                self.mem[a + i] &= bb                     # NOR program clears bits only
            self.ops.append(('program', a, len(buf)))
        self.wel = 0; self._pend = None; self._pbuf = bytearray()
    def xfer(self, b):
        self.tx.append(b); out = 0
        if self.cs == 1: return 0
        if self.st == 'cmd':
            self.cmd = b
            assert b in ALLOWED, f'DISALLOWED opcode {b:#x}'
            if b == 0x03: self.st = 'raddr'; self.al = 3; self.addr = 0
            elif b == 0x9f: self.st = 'jedec'; self.al = 0
            elif b == 0x05: self.st = 'rdsr'
            elif b == 0x06: self.wel = 1; self.st = 'ignore'          # WREN
            elif b == 0x20: self.st = 'eaddr'; self.al = 3; self.addr = 0
            elif b == 0x02: self.st = 'paddr'; self.al = 3; self.addr = 0
        elif self.st in ('raddr', 'eaddr', 'paddr'):
            self.addr = (self.addr << 8) | b; self.al -= 1
            if self.al == 0:
                if self.st == 'raddr': self.st = 'rdata'
                elif self.st == 'eaddr': self._pend = ('erase', self.addr); self.st = 'ignore'
                else: self._pend = ('program', self.addr); self._pbuf = bytearray(); self.st = 'pdata'
        elif self.st == 'pdata': self._pbuf.append(b)
        elif self.st == 'jedec': out = [0x5e, 0x40, 0x16][min(self.al, 2)]; self.al += 1
        elif self.st == 'rdsr': out = (self.wel << 1) | self.wip
        elif self.st == 'rdata': out = self.mem[self.addr & 0xffff]; self.addr += 1
        return out


class Sim(CPU):
    def __init__(self, flashdata, blob=None):
        self.flash = Flash(flashdata); self.r9000 = 0
        self.ok9248 = None                       # None = transfer always 'done'; int = N more done reads, then stuck
        super().__init__([(0x0, bytearray(0x10000), True)])
        if blob is None: blob = flstub.build()
        self.regions[0][1][0x2000:0x2000 + len(blob)] = blob
    def run(self, entry, stop=STOP, limit=5_000_000):
        pc = entry; self.r[9] = stop
        while pc != stop:
            self.steps += 1
            if self.steps > limit: raise RuntimeError(f'HANG: budget exhausted at pc={pc:#x}')
            w = self.access(pc, 4); op = w >> 26
            if op == 0x2d:   # mfspr
                d, a, k = (w >> 21) & 31, (w >> 16) & 31, w & 0xffff
                self.r[d] = self.rdspr((self.r[a] + k) & MASK); self.r[0] = 0; pc = (pc + 4) & MASK; continue
            if op == 0x30:   # mtspr
                a, b = (w >> 16) & 31, (w >> 11) & 31
                k = ((w >> 21) & 31) << 11 | (w & 0x7ff)
                self.wrspr((self.r[a] + k) & MASK, self.r[b] & MASK); pc = (pc + 4) & MASK; continue
            pc = self._step(pc, w)
    def rdspr(self, s):
        if s == 0x9248:
            if self.ok9248 is None: return 1
            if self.ok9248 > 0: self.ok9248 -= 1; return 1
            return 0                             # fault injection: transfer never completes -> byte() times out
        if s == 0x9250: return self.flash.rx
        if s == 0x9000: return self.r9000
        if s == 0x880c: return 0           # watchdog reads 0 (disabled) -> feed path skipped
        if s == 0x1800: return 0           # cache off
        return 0
    def wrspr(self, s, v):
        if s == 0x9000:
            self.r9000 = v; self.flash.csset(low=not (v & 0x4000))
        elif s == 0x9250:
            self.flash.rx = self.flash.xfer(v & 0xff)
        # 0x9244 start, 0x8xxx gates, 0x880c wdt: accept silently
    def _step(self, pc, w):
        r = self.r; op, d, a, b = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
        imm = w & 0xffff; nxt = (pc + 4) & MASK
        if op in (0, 3, 4):
            if op == 0 or (op == 3 and not self.flag) or (op == 4 and self.flag):
                nxt = (pc + signed(w & 0x3ffffff, 26) * 4) & MASK
        elif op == 1: r[9] = nxt; nxt = (pc + signed(w & 0x3ffffff, 26) * 4) & MASK
        elif op == 5: pass
        elif op == 6: r[d] = imm << 16
        elif op == 17: nxt = r[b]
        elif op == 18: nxt, r[9] = r[b], nxt
        elif op in (0x21, 0x23, 0x24, 0x25):
            size = {0x21: 4, 0x23: 1, 0x24: 1, 0x25: 2}[op]
            r[d] = self.access((r[a] + signed(imm, 16)) & MASK, size)
            if op == 0x24: r[d] = signed(r[d], 8)
        elif op == 0x27: r[d] = r[a] + signed(imm, 16)
        elif op == 0x29: r[d] = r[a] & imm
        elif op == 0x2a: r[d] = r[a] | imm
        elif op == 0x2b: r[d] = r[a] ^ signed(imm, 16)
        elif op == 0x2e:
            mode, sh = (w >> 6) & 3, w & 31
            r[d] = (r[a] << sh) if mode == 0 else (r[a] >> sh) if mode == 1 else (signed(r[a]) >> sh)
        elif op in (0x2f, 0x39):
            left = r[a]; right = (signed(imm, 16) & MASK) if op == 0x2f else r[b]; c = d
            if c >= 10: left, right = signed(left), signed(right)
            self.flag = {0: left == right, 1: left != right, 2: left > right, 3: left >= right,
                         4: left < right, 5: left <= right, 10: left > right, 11: left >= right,
                         12: left < right, 13: left <= right}[c]
        elif op in (0x35, 0x36, 0x37):
            off = signed(((w >> 21) & 31) << 11 | (w & 2047), 16)
            self.access((r[a] + off) & MASK, {0x35: 4, 0x36: 1, 0x37: 2}[op], r[b])
        elif op == 0x38:
            f = w & 0x7ff
            v = {0: r[a] + r[b], 2: r[a] - r[b], 3: r[a] & r[b], 4: r[a] | r[b], 5: r[a] ^ r[b],
                 8: r[a] << (r[b] & 31), 0x48: r[a] >> (r[b] & 31), 0x306: r[a] * r[b]}
            if f in v: r[d] = v[f]
            elif f == 0x30a: r[d] = r[a] // r[b] if r[b] else 0
            else: raise ValueError(f'ALU {f:#x}@{pc:#x}')
        else: raise ValueError(f'op {w:08x}@{pc:#x}')
        self.r = [x & MASK for x in r]; self.r[0] = 0
        return nxt


def req(cpu, command, addr, length, payload=b''):
    cpu.access(REQ + 0, 4, 0x52585041); cpu.access(REQ + 4, 4, 1); cpu.access(REQ + 8, 4, command)
    cpu.access(REQ + 12, 4, addr); cpu.access(REQ + 16, 4, length); cpu.access(REQ + 20, 4, 0xDEAD); cpu.access(REQ + 24, 4, 0)
    for i, bb in enumerate(payload): cpu.access(REQ + 28 + i, 1, bb)


def run_cmd(cpu, command, addr, length, payload=b'', limit=5_000_000):
    req(cpu, command, addr, length, payload); cpu.r[3] = REQ
    cpu.flash.tx = []; cpu.steps = 0
    cpu.run(0x2000, limit=limit)
    status = cpu.access(REQ + 20, 4)
    data = bytes(cpu.access(REQ + 28 + i, 1) for i in range(length))
    return status, data, cpu.flash.tx, cpu.steps


BACKUP = os.environ.get('KDC_DUMP', '')   # optional real 4MB dump; a synthetic 64K is used if unset
SECTOR = 0x9000
PATCH = {0x9f48: 0xc8, 0x9f49: 0x00}   # 1000 -> 200 ticks


def main():
    if BACKUP:
        if not os.path.exists(BACKUP): sys.exit(f'KDC_DUMP set but not found: {BACKUP} (unset it to use the synthetic image)')
        backup = open(BACKUP, 'rb').read(); print(f'using real dump: {BACKUP}')
    else:
        # synthetic 64 KiB test image: blank except a deterministic sector 0x9000 carrying the real
        # delay instruction at 0x9f48, so the write-flow/bounds/error tests run without the vendor dump.
        b = bytearray(b'\xff' * 0x10000)
        for i in range(0x9000, 0xa000): b[i] = (i * 7) & 0xff
        struct.pack_into('<I', b, 0x9f48, 0x9e5803e8)        # l.addi r18,r24,1000
        backup = bytes(b); print('using synthetic 64K test image (set KDC_DUMP=<4MB dump> to use the real one)')
    win = bytearray(backup[0:0x10000])                       # modeled 64 KiB window (covers sector 0x9000)
    orig_sector = bytes(win[SECTOR:SECTOR + 0x1000])
    patched_sector = bytearray(orig_sector)
    for a, v in PATCH.items(): patched_sector[a - SECTOR] = v

    print('=== cmd0 (init + JEDEC) ===')
    cpu = Sim(win)
    st, data, tx, steps = run_cmd(cpu, 0, 0, 3)
    print(f'  {steps} steps; status=0x{st:x}; JEDEC={data.hex()} (expect 5e4016); ok={data == bytes([0x5e,0x40,0x16])}')

    print('=== cmd1 (read the patch window @0x9f40, before any write) ===')
    cpu = Sim(win)
    st, data, tx, steps = run_cmd(cpu, 1, 0x9f40, 16)
    print(f'  status=0x{st:x}; data={data.hex()} == backup? {data == backup[0x9f40:0x9f50]}')

    print('=== FULL write flow on ONE device: erase 0x9000 -> program 16 pages (patched) -> read back ===')
    cpu = Sim(win)
    st, _, _, _ = run_cmd(cpu, 0, 0, 3)                        # init first (as the host does)
    # erase
    st, _, tx, steps = run_cmd(cpu, 3, SECTOR, 0)
    erased = cpu.flash.mem[SECTOR:SECTOR + 0x1000]
    print(f'  erase: status=0x{st:x}; sector all-0xFF after erase? {erased == bytes([0xff])*0x1000}')
    # program 16 pages from the patched sector
    pstatus = []
    for p in range(16):
        a = SECTOR + p * 256
        st, _, _, _ = run_cmd(cpu, 2, a, 256, bytes(patched_sector[p*256:(p+1)*256]))
        pstatus.append(st)
    print(f'  program 16 pages: all status 0? {all(s == 0 for s in pstatus)}  statuses={[hex(s) for s in pstatus]}')
    # read back the whole sector via the payload's own read path, in 256B chunks
    readback = bytearray()
    for p in range(16):
        st, d, _, _ = run_cmd(cpu, 1, SECTOR + p*256, 256)
        readback += d
    print(f'  readback == patched_sector? {bytes(readback) == bytes(patched_sector)}')
    print(f'  readback == original (unpatched)? {bytes(readback) == orig_sector}  (must be False)')
    print(f'  bytes that differ from original: {[hex(SECTOR+i) for i in range(0x1000) if readback[i] != orig_sector[i]]}')
    print(f'  model flash[0x9f48:0x9f4a] = {cpu.flash.mem[0x9f48:0x9f4a].hex()} (expect c800)')

    # SAFETY ASSERTIONS
    print('=== SAFETY ASSERTIONS ===')
    ops = cpu.flash.ops
    erases = [o for o in ops if o[0] == 'erase']
    programs = [o for o in ops if o[0] == 'program']
    assert all(a == SECTOR for _, a, _ in erases), f'erase outside target sector: {erases}'
    assert all(SECTOR <= a < SECTOR + 0x1000 for _, a, _ in programs), f'program outside target sector: {programs}'
    assert len(erases) == 1, f'expected exactly 1 erase, got {len(erases)}'
    # nothing outside sector 0x9000 was touched in the model memory:
    untouched = win[:SECTOR] + win[SECTOR+0x1000:]
    now = cpu.flash.mem[:SECTOR] + cpu.flash.mem[SECTOR+0x1000:]
    assert untouched == now, 'memory OUTSIDE sector 0x9000 changed!'
    assert bytes(readback) == bytes(patched_sector), 'readback mismatch'
    assert bytes(readback) != orig_sector, 'patch had no effect'
    print(f'  OK: 1 erase @0x{SECTOR:x}, {len(programs)} programs all in-sector, nothing outside sector 0x9000 changed.')
    print(f'  OK: only 2 bytes changed vs backup: 0x9f48 e8->c8, 0x9f49 03->00.')

    print('=== BOUNDS GUARD (payload must REJECT out-of-window / malformed, status != 0, no write) ===')
    def rejected(cmd, addr, n, payload=b''):
        c = Sim(win)
        before = bytes(c.flash.mem)
        st, _, _, _ = run_cmd(c, cmd, addr, n, payload)
        ops = list(c.flash.ops)
        touched = bytes(c.flash.mem) != before
        return st, ops, touched
    cases = [
        ('erase @0x8000 (below window)', 3, 0x8000, 0, b''),
        ('erase @0xa000 (above window)', 3, 0xa000, 0, b''),
        ('erase @0x9100 (not 4K-aligned)', 3, 0x9100, 0, b''),
        ('program @0x9000 len 300 (>256)', 2, 0x9000, 300, bytes(300)),
        ('program @0x90f0 len 32 (page cross)', 2, 0x90f0, 32, bytes(32)),
        ('program @0x8000 (below window)', 2, 0x8000, 16, bytes(16)),
    ]
    all_rejected = True
    for name, c, a, n, pl in cases:
        st, ops, touched = rejected(c, a, n, pl)
        ok = (st != 0) and (not ops) and (not touched)
        all_rejected &= ok
        print(f'  {name}: status=0x{st:x} ops={ops} touched={touched}  -> {"REJECTED OK" if ok else "!!! NOT REJECTED"}')
    assert all_rejected, 'a malformed/out-of-window request was NOT rejected'

    print('=== ERROR PROPAGATION (byte() timeout must NOT report success) — the bug Codex found ===')
    # Let the first RDSR transfer (read-status) time out; payload must return status != 0, not stale 0.
    c = Sim(win); c.ok9248 = 0            # 0 done-reads allowed: the very first 0x9248 poll is stuck
    st, _, _, steps = run_cmd(c, 3, SECTOR, 0, limit=12_000_000)
    print(f'  erase with stuck transfer: status=0x{st:x} (expect 2 = byte timeout), terminated in {steps} steps')
    assert st == 2, 'byte() timeout was swallowed or mis-reported (regression of Codex finding #1)'
    assert c.flash.ops == [], 'a write happened despite the transfer fault'

    print('=== WINDOW PARAMETERISATION (a payload built for a different window enforces it) ===')
    scratch_blob = flstub.build(window=(0x3000, 0x4000))
    c = Sim(win, blob=scratch_blob)
    st, _, _, _ = run_cmd(c, 3, 0x3000, 0)        # inside the scratch window -> allowed
    st2, _, _, _ = run_cmd(Sim(win, blob=scratch_blob), 3, SECTOR, 0)   # 0x9000 now OUTSIDE -> rejected
    print(f'  window (0x3000,0x4000): erase 0x3000 status=0x{st:x} (allowed), erase 0x9000 status=0x{st2:x} (rejected)')
    assert st == 0 and st2 == 1, 'window parameterisation not enforced'

    print('  ALL ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
