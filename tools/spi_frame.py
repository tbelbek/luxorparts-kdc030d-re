#!/usr/bin/env python3
"""Framing check: run the ROM read primitives and record an ordered timeline of
SPR 0x9000 writes (bit 0x4000 = CS/active per 0x104898 clear / 0x1048c8 set) and
SPR 0x9250 TX bytes. Determines whether the whole 0x03+addr+data read is ONE
continuous CS-low transaction (=> only 0x03 is ever a command => safe) or whether
CS toggles mid-stream (=> an address/data byte could be framed as a command)."""
import os, sys
sys.path.insert(0, os.path.join(os.environ.get('KDC_PENGUIN_REPO',''), 'tools'))
from or1k_subset import CPU, MASK, signed  # noqa

ROM = os.path.join(os.environ.get('KDC_ROM_DIR', os.path.join(os.path.dirname(__file__),'..','refs')), 'soc_boot_rom.bin')
STOP = 0xfffffffc


class H(CPU):
    def __init__(self, regions):
        super().__init__(regions)
        self.timeline = []       # ('cs', val9000) or ('tx', byte)
        self.reg9000 = 0
        self.rx = 0xA5

    def run(self, entry, stop=STOP, limit=4_000_000):
        pc = entry; self.r[9] = stop
        while pc != stop:
            self.steps += 1
            if self.steps > limit:
                raise ValueError(f'budget/hang at {pc:#x}')
            w = self.access(pc, 4); op = w >> 26
            if op == 0x2d:   # mfspr rD,rA,K
                d, a, k = (w >> 21) & 31, (w >> 16) & 31, w & 0xffff
                spr = (self.r[a] + k) & MASK
                self.r[d] = 1 if spr == 0x9248 else self.rx if spr == 0x9250 else self.reg9000 if spr == 0x9000 else 0
                self.r[0] = 0; pc = (pc + 4) & MASK; continue
            if op == 0x30:   # mtspr rA,rB,K
                a, b = (w >> 16) & 31, (w >> 11) & 31
                k = ((w >> 21) & 31) << 11 | (w & 0x7ff)
                spr = (self.r[a] + k) & MASK; val = self.r[b] & MASK
                if spr == 0x9000:
                    self.reg9000 = val; self.timeline.append(('cs', val))
                elif spr == 0x9250:
                    self.timeline.append(('tx', val & 0xff))
                pc = (pc + 4) & MASK; continue
            pc = self._one(pc, w)

    def _one(self, pc, w):
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
        self.r = [v & MASK for v in r]; self.r[0] = 0
        return nxt


def build():
    rom = open(ROM, 'rb').read()
    c = H([(0, bytearray(0x10000), True), (0x100000, bytearray(rom), False)])
    c.access(0x4f74 + 0x1c, 4, 0x0313c00f)   # [0x4f93]=0x03
    c.access(0x4f74 + 0x15, 1, 0x80)
    for o in (0x14, 0x16, 0x17): c.access(0x4f74 + o, 1, 0)
    return c


def call(c, addr, **regs):
    c.r[1] = 0xf000
    for k, v in regs.items(): c.r[int(k[1:])] = v & MASK
    c.run(addr)


c = build()
call(c, 0x101018)
call(c, 0x100ef0, r3=0x00012345)     # cmd+addr (address value arbitrary for framing)
call(c, 0x100ca0, r3=0x6000, r4=1)   # read 512 bytes
# Analyze framing: find CS(0x4000) state across the timeline, check it stays asserted
# from the first TX (the 0x03 command) through the last TX byte.
cs_bit = lambda v: (v >> 14) & 1
events = c.timeline
# compact print: collapse the 512 identical 0xFF tx into a count
compact = []
i = 0
while i < len(events):
    kind, val = events[i]
    if kind == 'tx' and val == 0xff:
        n = 0
        while i < len(events) and events[i] == ('tx', 0xff): n += 1; i += 1
        compact.append(('tx', f'0xff x{n}')); continue
    compact.append((kind, f'0x{val:04x}' if kind == 'cs' else f'0x{val:02x}')); i += 1
print('timeline (cs = SPR0x9000 write, bit14=CS/active):')
for k, v in compact: print(f'  {k}: {v}' + (f'   [CS bit14={cs_bit(int(v,16))}]' if k == 'cs' else ''))
# safety test: between the first tx and the last tx, does bit14 ever change?
first = next(i for i, e in enumerate(events) if e[0] == 'tx')
last = max(i for i, e in enumerate(events) if e[0] == 'tx')
cs_vals_during = [cs_bit(v) for i, (k, v) in enumerate(events) if k == 'cs' and first <= i <= last]
print(f'\nTX bytes total: {sum(1 for k,_ in events if k=="tx")}')
print(f'distinct TX byte values: {sorted({v for k,v in events if k=="tx"})}')
print(f'CS(bit14) writes BETWEEN first and last TX: {cs_vals_during}  '
      f'=> {"TOGGLES mid-stream (UNSAFE framing)" if any(x==1 for x in cs_vals_during) or (0 in cs_vals_during and len(set(cs_vals_during))>1) else "no mid-stream re-assert"}')
