#!/usr/bin/env python3
"""Hand-assemble two OpenRISC (JT5295 no-delay-slot, little-endian) SRAM payloads
for the forced BootROM loader:

  marker  : zero-SPI probe. r3 = param block; stores 0x0000CAFE at [r3+12]; returns.
            Proves the loader launches an SRAM payload (callback) and it returns,
            touching NO SPI and NO flash.
  reader  : READ-ONLY flash reader. Orchestrates verified ROM primitives:
            0x101018 gate-enable, 0x10485c one-byte SPI exchange; CS via SPR 0x9000
            bit 0x4000 (clear=assert, set=deassert, exactly as ROM 0x104898/0x1048c8).
            Sends ONLY {0x03, three address bytes, 0xFF}. No 0x06/0x20/0xD8/0x02/0x01/0x50
            opcode is emitted or even present in the code. Worst case: a hang (the flash
            cannot be modified without a WREN we never send). Params at [r3]:
              +0 flash_offset  +4 length(<=480)  +8 dest(SRAM)  +12 status(out)

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
    def jal(self, target): self._emit(1 << 26 | (((target - (self.base + 4 * len(self.w))) >> 2) & 0x3ffffff))
    def jr(self, b): self._emit(0x11 << 26 | b << 11)
    def sfgeu(self, a, b): self._emit(0x39 << 26 | 3 << 21 | a << 16 | b << 11)
    def sfeqi(self, a, imm): self._emit(0x2f << 26 | 0 << 21 | a << 16 | (imm & 0xffff))
    def sfnei(self, a, imm): self._emit(0x2f << 26 | 1 << 21 | a << 16 | (imm & 0xffff))
    def bf(self, n): self._mark(n, 4)
    def bnf(self, n): self._mark(n, 3)
    def j(self, n): self._mark(n, 0)
    def _mark(self, name, op): self.fix.append((len(self.w), op, name)); self._emit(0)
    def nop(self): self._emit(0x15000000)   # l.nop (op 0x05), not 0x15<<26
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


def reader():
    a = Asm(ENTRY)
    # prologue: stack at 0x37f0; save r9 (link) and r3 (param ptr) in callee-saved r14,r16
    a.const(1, 0x37f0)
    a.ori(14, 9, 0)         # r14 = return address
    a.ori(16, 3, 0)         # r16 = param block
    a.lwz(17, 16, 0)        # r17 = flash_offset
    a.lwz(18, 16, 4)        # r18 = length
    a.lwz(20, 16, 8)        # r20 = dest
    a.jal(GATE); a.nop()    # enable SPI clock gates (ROM, no SPI bytes)
    # CS assert: clear bit 0x4000 of SPR 0x9000
    a.mfspr(7, 0, SPR9000); a.const(8, 0xFFFFBFFF); a.add(7, 7, 0); a._emit(0x38 << 26 | 7 << 21 | 7 << 16 | 8 << 11 | 3)  # r7 = r7 & r8
    a.mtspr(0, 7, SPR9000)
    # send READ opcode 0x03
    a.addi(3, 0, 0x03); a.jal(XCHG); a.nop()
    # send 3 address bytes, MSB first  (standard 25-series READ addressing)
    a.ori(5, 17, 0)                 # r5 = offset (working copy)
    a.mfspr(3, 5, 0) if False else None
    a._emit(0x2e << 26 | 3 << 21 | 5 << 16 | (16) | (1 << 6))   # r3 = r5 >> 16  (l.srli r3,r5,16)
    a.andi(3, 3, 0xFF); a.jal(XCHG); a.nop()
    a._emit(0x2e << 26 | 3 << 21 | 5 << 16 | (8) | (1 << 6))    # r3 = r5 >> 8
    a.andi(3, 3, 0xFF); a.jal(XCHG); a.nop()
    a.ori(3, 5, 0); a.andi(3, 3, 0xFF); a.jal(XCHG); a.nop()    # r3 = offset & 0xFF
    # read loop: for r19 in 0..length: RX = xchg(0xFF); dest[r19] = RX
    a.addi(19, 0, 0)
    a.label('loop')
    a.sfgeu(19, 18); a.bf('done')   # if r19 >= length: done
    a.nop()
    a.addi(3, 0, 0xFF); a.jal(XCHG); a.nop()   # r11 = RX
    a.add(4, 20, 19); a.sb(4, 0, 11)           # dest[r19] = RX
    a.addi(19, 19, 1); a.j('loop'); a.nop()
    a.label('done')
    # CS deassert: set bit 0x4000 of SPR 0x9000
    a.mfspr(7, 0, SPR9000); a.ori(7, 7, 0x4000); a.mtspr(0, 7, SPR9000)
    # status = 0x0000600D (done)
    a.const(4, 0x0000600D); a.sw(16, 12, 4)
    a.jr(14); a.nop()               # return to loader via saved link
    return a.finish()


if __name__ == '__main__':
    import subprocess
    for name, blob in (('marker', marker()), ('reader', reader())):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'stub_{name}.bin')
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
