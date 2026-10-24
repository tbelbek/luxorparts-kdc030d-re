#!/usr/bin/env python3
"""Offline validation of re/flasher.bin (the hand-ported flasher payload): run it on a
modeled OpenRISC CPU + SPI controller + fake flash + request block, and check it
TERMINATES and performs a correct flash read (right TX bytes, right data, status 0).
Catches the infinite-loop/fault bugs that broke earlier hand-assembled stubs — offline."""
import struct, sys
sys.path.insert(0, '/private/tmp/claude-501/-Users-tbelbek--claude-spares-spare-ifmGrF/3f8797bd-678d-4de2-97be-a71b1a559292/scratchpad/dl-penguin/repo/tools')
from or1k_subset import CPU, MASK, signed

BIN = '/private/tmp/claude-501/-Users-tbelbek--claude-spares-spare-ifmGrF/3f8797bd-678d-4de2-97be-a71b1a559292/scratchpad/re/flasher.bin'
STOP = 0xfffffffc
REQ = 0x3400
FLASH = bytes((0x100 + i) & 0xff if i >= 4 else b'BLDR'[i] for i in range(0x1000))  # recognizable pattern


class Flash:
    def __init__(self): self.cs = 1; self.st = 'idle'; self.cmd = 0; self.addr = 0; self.al = 0; self.rx = 0; self.tx = []
    def csset(self, low):
        if low and self.cs == 1: self.st = 'cmd'
        self.cs = 0 if low else 1
    def xfer(self, b):
        self.tx.append(b); out = 0
        if self.cs == 1: return 0
        if self.st == 'cmd':
            self.cmd = b
            if b == 0x03: self.st = 'addr'; self.al = 3; self.addr = 0
            elif b == 0x9f: self.st = 'jedec'; self.al = 0
            else: self.st = 'data'
        elif self.st == 'addr':
            self.addr = (self.addr << 8) | b; self.al -= 1
            if self.al == 0: self.st = 'data'
        elif self.st == 'jedec':
            out = [0x5e, 0x40, 0x16][min(self.al, 2)]; self.al += 1
        elif self.st == 'data':
            out = FLASH[self.addr & 0xfff]; self.addr += 1
        return out


class Sim(CPU):
    def __init__(self):
        self.flash = Flash(); self.r9000 = 0
        super().__init__([(0x0, bytearray(0x10000), True)])
        open(BIN, 'rb')  # ensure present
        blob = open(BIN, 'rb').read()
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
        if s == 0x9248: return 1
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


def req(cpu, command, addr, length):
    cpu.access(REQ + 0, 4, 0x52585041); cpu.access(REQ + 4, 4, 1); cpu.access(REQ + 8, 4, command)
    cpu.access(REQ + 12, 4, addr); cpu.access(REQ + 16, 4, length); cpu.access(REQ + 20, 4, 0xDEAD); cpu.access(REQ + 24, 4, 0)


def go(command, addr, length):
    cpu = Sim(); req(cpu, command, addr, length); cpu.r[3] = REQ
    cpu.flash.tx = []
    cpu.run(0x2000)
    status = cpu.access(REQ + 20, 4)
    data = bytes(cpu.access(REQ + 28 + i, 1) for i in range(length))
    return status, data, cpu.flash.tx, cpu.steps


if __name__ == '__main__':
    print('=== cmd0 (init + JEDEC) ===')
    st, data, tx, steps = go(0, 0, 3)
    print(f'  terminated in {steps} steps; status=0x{st:x}; JEDEC data={data.hex()}; first TX={[hex(b) for b in tx[:6]]}')
    print('=== cmd1 (read 256 @ 0) ===')
    st, data, tx, steps = go(1, 0, 256)
    exp = bytes(FLASH[i] for i in range(256))
    print(f'  terminated in {steps} steps; status=0x{st:x}')
    print(f'  TX: first8={[hex(b) for b in tx[:8]]}  distinct={sorted(set(hex(b) for b in tx))}  count={len(tx)}')
    print(f'  data==flash? {data == exp}')
    print(f'  data[:16]={data[:16].hex()}  expected[:16]={exp[:16].hex()}')
