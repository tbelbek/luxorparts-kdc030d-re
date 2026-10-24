#!/usr/bin/env python3
"""Hand-assemble the OpenRISC (JT5295 no-delay-slot, little-endian) marker SRAM payload
for the forced BootROM loader:

  marker  : zero-SPI probe. r3 = param block; stores 0x0000CAFE at [r3+12]; returns.
            Proves the loader launches an SRAM payload (callback) and it returns,
            touching NO SPI and NO flash.

(The read/erase/program SPI payload that ships lives in tools/flstub.py, window-guarded
and offline-validated by tools/flsim.py; the earlier reader/diag bring-up probes were
retired once flstub/dumpflash superseded them.)

Entry convention (from apxtool rom_rescue / rom-rescue.S): the loader calls the
payload at its first instruction with r3 = param-block pointer, link in r9; the
payload returns with l.jr r9. Payload lives at SRAM 0x2000.
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GATE = 0x00101018          # ROM: enable SPI controller clock gates (no SPI bytes)
XCHG = 0x0010485c          # ROM: one-byte SPI exchange (TX r3 -> RX r11)
SPR9000 = 0x9000           # SPI controller control; bit 0x4000 = CS (0=assert)
ENTRY = 0x2000


class Asm:
    def __init__(self, base):
        self.base = base; self.w = []; self.labels = {}; self.fix = []
    def _emit(self, word): self.w.append(word & 0xffffffff)
    def label(self, n): self.labels[n] = self.base + 4 * len(self.w)
    # -- ALU / immediate --
    def movhi(self, d, imm): self._emit(0x6 << 26 | d << 21 | (imm & 0xffff))
    def ori(self, d, a, imm): self._emit(0x2a << 26 | d << 21 | a << 16 | (imm & 0xffff))
    def andi(self, d, a, imm): self._emit(0x29 << 26 | d << 21 | a << 16 | (imm & 0xffff))
    def addi(self, d, a, imm): self._emit(0x27 << 26 | d << 21 | a << 16 | (imm & 0xffff))
    def add(self, d, a, b): self._emit(0x38 << 26 | d << 21 | a << 16 | b << 11)
    def const(self, d, v):   # load 32-bit constant
        self.movhi(d, (v >> 16) & 0xffff)
        if v & 0xffff: self.ori(d, d, v & 0xffff)
    def lwz(self, d, a, off): self._emit(0x21 << 26 | d << 21 | a << 16 | (off & 0xffff))
    def lbz(self, d, a, off): self._emit(0x23 << 26 | d << 21 | a << 16 | (off & 0xffff))
    def sw(self, a, off, b): self._emit(0x35 << 26 | ((off >> 11) & 31) << 21 | a << 16 | b << 11 | (off & 0x7ff))
    def sb(self, a, off, b): self._emit(0x36 << 26 | ((off >> 11) & 31) << 21 | a << 16 | b << 11 | (off & 0x7ff))
    def mfspr(self, d, a, k): self._emit(0x2d << 26 | d << 21 | a << 16 | (k & 0xffff))
    def mtspr(self, a, b, k): self._emit(0x30 << 26 | ((k >> 11) & 31) << 21 | a << 16 | b << 11 | (k & 0x7ff))
    def and_(self, d, a, b): self._emit(0x38 << 26 | d << 21 | a << 16 | b << 11 | 3)
    def or_(self, d, a, b): self._emit(0x38 << 26 | d << 21 | a << 16 | b << 11 | 4)
    def srli(self, d, a, sh): self._emit(0x2e << 26 | d << 21 | a << 16 | (sh & 31) | (1 << 6))
    def jal(self, target): self._emit(1 << 26 | (((target - (self.base + 4 * len(self.w))) >> 2) & 0x3ffffff))
    def jall(self, n): self._mark(n, 1)     # l.jal to a (possibly forward) label
    def jr(self, b): self._emit(0x11 << 26 | b << 11)
    def sfne(self, a, b): self._emit(0x39 << 26 | 1 << 21 | a << 16 | b << 11)
    def sfgeui(self, a, imm): self._emit(0x2f << 26 | 3 << 21 | a << 16 | (imm & 0xffff))
    def sfgeu(self, a, b): self._emit(0x39 << 26 | 3 << 21 | a << 16 | b << 11)
    def sfeqi(self, a, imm): self._emit(0x2f << 26 | 0 << 21 | a << 16 | (imm & 0xffff))
    def sfnei(self, a, imm): self._emit(0x2f << 26 | 1 << 21 | a << 16 | (imm & 0xffff))
    def bf(self, n): self._mark(n, 4)
    def bnf(self, n): self._mark(n, 3)
    def j(self, n): self._mark(n, 0)
    def _mark(self, name, op): self.fix.append((len(self.w), op, name)); self._emit(0)
    def nop(self): self._emit(0x15000000)   # l.nop (op 0x05), not 0x15<<26
    # SPR constant-address helpers (scratch: r2,r3)
    def wrspr(self, spr, val): self.const(2, val); self.mtspr(0, 2, spr)
    def updspr(self, spr, mask, val):       # SPR = (SPR & ~mask) | (val & mask)
        self.mfspr(2, 0, spr); self.const(3, (~mask) & 0xffffffff); self.and_(2, 2, 3)
        self.const(3, val & mask); self.or_(2, 2, 3); self.mtspr(0, 2, spr)
    def finish(self):
        for idx, op, name in self.fix:
            disp = (self.labels[name] - (self.base + 4 * idx)) >> 2
            self.w[idx] = (op << 26 | (disp & 0x3ffffff)) & 0xffffffff
        return b''.join(struct.pack('<I', x) for x in self.w)


def marker():
    a = Asm(ENTRY)
    a.const(4, 0x0000CAFE)
    a.sw(3, 12, 4)          # [param+12] = 0xCAFE
    a.jr(9)                 # return to loader (r9 = link)
    a.nop()
    return a.finish()


# Faithful port of the hardware-verified READ path in matu's rom-rescue.c:
#   command 0 (!(a&1)) SPI-controller init, then command 1 READ (cs,0x03+addr,data,cs).
# Only ever clocks {0x03, address bytes, 0xFF}; contains no WREN/erase/program opcode.
# "Existing state" path (rom-rescue.c command-0 a&1 / apxtool spi_init="rom"): do NO SPI
# init at all — no clock gates, no 0x9000 controller config. Use the SPI state the BootROM
# already programmed; only toggle CS + direction bits. The clock/0x9000 reconfiguration is
# exactly what broke USB in the 0xDA loader on the earlier attempts, so we omit it entirely.
# Minimal init: SPI-block clock GATES (needed or SPI-register access bus-faults) but NOT
# the PLL/clock-mux writes. Retested with the fixed harness (30s launch + read from the
# launch response) after the no-init run showed status=0 (fault: SPI block unclocked).
INIT_UPD = [(0x8100, 0xd00, 0xd00), (0x8040, 0xd00, 0), (0x8000, 0x400, 0x400)]
INIT_WR = [(0x9000, 0x01115005), (0x9004, 0), (0x9008, 0), (0x900c, 0),
           (0x9244, 0x03ff0000), (0x9248, 0), (0x924c, 5)] \
    + [(r, 0) for r in range(0x9250, 0x9280, 4)] \
    + [(0x9260, 0xffff), (0x9264, 0xffffffff), (0x926c, 0x0fffffff), (0x9270, 0x0fffffff)]
# Kept for reference: the full/minimal init sequences that broke USB after 0xDA.
INIT_UPD_FULL = [(0x8850, 0xf0000000, 0x30000000), (0x8844, 0x8, 0), (0x8840, 0x100000, 0),
                 (0x8344, 0x300000, 0x100000), (0x8100, 0xd00, 0xd00), (0x8040, 0xd00, 0), (0x8000, 0x400, 0x400)]
INIT_WR_FULL = ([(0x9000, 0x01115005), (0x9004, 0), (0x9008, 0), (0x900c, 0),
            (0x9244, 0x03ff0000), (0x9248, 0), (0x924c, 5)]
           + [(r, 0) for r in range(0x9250, 0x9280, 4)]            # zero 0x9250..0x927c (as the C loop does)
           + [(0x9260, 0xffff), (0x9264, 0xffffffff), (0x926c, 0x0fffffff), (0x9270, 0x0fffffff)])  # then overrides


# Every SPR the stub writes; saved at entry and restored before returning so the loader's
# USB/0xCB read path is left exactly as we found it (then a separate 0xCB read of our SRAM
# result works, as the marker proved — no cache flush, no clobbered controller state).
SAVED_SPRS = [0x880c, 0x8100, 0x8040, 0x8000, 0x9000, 0x9004, 0x9008, 0x900c,
              0x9244, 0x9248, 0x924c] + list(range(0x9250, 0x9280, 4))
SAVE_AREA = 0x3600


if __name__ == '__main__':
    import subprocess
    out_dir = os.path.dirname(os.path.abspath(__file__))            # write beside this script (tools/)
    for name, blob in (('marker', marker()),):
        path = os.path.join(out_dir, f'stub_{name}.bin')
        open(path, 'wb').write(blob)
        print(f'=== {name}: {len(blob)} bytes ===')
        # self-verify by disassembling
        r = subprocess.run(['python3', '-I',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'or1k_dis.py'),
            path, f'{ENTRY:x}', '0', f'{len(blob):x}'], capture_output=True, text=True)
        print(r.stdout or r.stderr)
        # scan for any dangerous opcode byte baked as an immediate 0x06/0x20/0xd8/0xc7/0x02/0x01/0x50 to r3 before an XCHG
        print('  opcode-bytes ever loaded for TX (addi r3,r0,IMM):',
              [hex(struct.unpack_from('<I', blob, i)[0] & 0xffff) for i in range(0, len(blob), 4)
               if (struct.unpack_from('<I', blob, i)[0] >> 26) == 0x27
               and ((struct.unpack_from('<I', blob, i)[0] >> 21) & 31) == 3
               and ((struct.unpack_from('<I', blob, i)[0] >> 16) & 31) == 0])
