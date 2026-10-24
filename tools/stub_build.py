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


def diag(spr):
    """Minimal returning stub: sentinel -> read one SPR into param+16 -> DONE. Isolates whether
    reading SPR `spr` faults/hangs. Retrieved via a separate read (as the marker proved works)."""
    a = Asm(ENTRY)
    a.const(1, 0x37f0)
    a.ori(14, 9, 0)                    # save loader return
    a.ori(16, 3, 0)                    # r16 = param
    a.const(4, 0xD1A60000); a.sw(16, 12, 4)   # stage sentinel (reached entry)
    a.mfspr(5, 0, spr); a.sw(16, 16, 5)       # <-- the SPR under test
    a.const(4, 0x0000D04E); a.sw(16, 12, 4)   # DONE (reached the end -> no fault)
    a.jr(14); a.nop()
    return a.finish()


def reader():
    a = Asm(ENTRY)
    a.const(1, 0x37f0)                 # stack
    a.ori(14, 9, 0)                    # r14 = loader return address
    a.ori(16, 3, 0)                    # r16 = param block
    a.const(15, SAVE_AREA)             # r15 = SPR save area
    for i, spr in enumerate(SAVED_SPRS):           # snapshot originals BEFORE touching anything
        a.mfspr(5, 0, spr); a.sw(15, i * 4, 5)
    a.wrspr(0x880c, 0x5500)            # disable watchdog (reads back 0 = off)
    for spr, m, v in INIT_UPD: a.updspr(spr, m, v)
    for spr, v in INIT_WR: a.wrspr(spr, v)
    a.lwz(17, 16, 0)                   # r17 = flash_offset
    a.lwz(18, 16, 4)                   # r18 = length
    a.lwz(20, 16, 8)                   # r20 = dest

    def send(setup):
        setup()                        # leave the byte in r3
        a.addi(4, 0, 0); a.jall('byte'); a.nop()
        a.sfnei(8, 0); a.bf('fail'); a.nop()

    # JEDEC-ID probe (0x9f -> 3 bytes) into param+16..18: confirms SPI works at all.
    a.updspr(SPR9000, 0x4000, 0)       # CS assert
    send(lambda: a.addi(3, 0, 0x9f))
    for off in (16, 17, 18):
        a.addi(3, 0, 0xff); a.addi(4, 0, 0x80); a.jall('byte'); a.nop()
        a.sfnei(8, 0); a.bf('fail'); a.nop()
        a.sb(16, off, 11)              # param[off] = jedec byte
    a.updspr(SPR9000, 0x4000, 0x4000)  # CS deassert

    # main READ: CS assert, 0x03 + 3 address bytes (MSB first), data, CS deassert
    a.updspr(SPR9000, 0x4000, 0)       # CS assert (clear bit 0x4000)
    send(lambda: a.addi(3, 0, 0x03))
    send(lambda: (a.srli(3, 17, 16), a.andi(3, 3, 0xff)))
    send(lambda: (a.srli(3, 17, 8), a.andi(3, 3, 0xff)))
    send(lambda: (a.ori(3, 17, 0), a.andi(3, 3, 0xff)))
    a.addi(19, 0, 0)                   # i = 0
    a.label('loop')
    a.sfgeu(19, 18); a.bf('rdone'); a.nop()        # i >= length -> done
    a.addi(3, 0, 0xff); a.addi(4, 0, 0x80); a.jall('byte'); a.nop()   # recv a byte
    a.sfnei(8, 0); a.bf('fail'); a.nop()
    a.add(6, 20, 19); a.sb(6, 0, 11)               # dest[i] = RX
    a.addi(19, 19, 1); a.j('loop'); a.nop()
    a.label('rdone')
    a.updspr(SPR9000, 0x4000, 0x4000)              # CS deassert
    a.const(4, 0x0000600D); a.sw(16, 12, 4)        # status = ok
    a.jall('restore'); a.nop()                     # put every touched SPR back so USB/reads stay pristine
    a.jr(14); a.nop()
    a.label('fail')
    a.updspr(SPR9000, 0x4000, 0x4000)              # release the bus
    a.const(3, 0xBAD00000); a.or_(3, 3, 8); a.sw(16, 12, 3)   # status = 0xBAD0000 | errcode
    a.jall('restore'); a.nop()
    a.jr(14); a.nop()
    # --- restore(): write the saved original values back to every SPR we modified ---
    a.label('restore')
    for i, spr in enumerate(SAVED_SPRS):
        a.lwz(5, 15, i * 4); a.mtspr(0, 5, spr)    # r15 = save area; SPR = saved[i]
    a.jr(9); a.nop()
    # --- byte(r3=value, r4=recv-bits 0 or 0x80) -> r11=RX, r8=0 ok / 2 timeout ---
    a.label('byte')
    a.mfspr(5, 0, SPR9000); a.const(2, 0xFFFFFF7F); a.and_(5, 5, 2); a.or_(5, 5, 4); a.mtspr(0, 5, SPR9000)
    a.mtspr(0, 3, 0x9250)              # TX value
    a.addi(6, 0, 1); a.mtspr(0, 6, 0x9244)         # start
    a.const(7, 0x00080000)            # bounded timeout (returns rather than hangs)
    a.label('poll')
    a.mfspr(2, 0, 0x880c); a.sfeqi(2, 0); a.bf('nofeed'); a.nop()   # feed watchdog so a wait can't reset us
    a.const(2, 0x0005001d); a.mtspr(0, 2, 0x880c)
    a.label('nofeed')
    a.mfspr(5, 0, 0x9248); a.andi(5, 5, 1); a.sfnei(5, 0); a.bf('bgot'); a.nop()
    a.addi(7, 7, -1); a.sfeqi(7, 0); a.bf('btmo'); a.nop()
    a.j('poll'); a.nop()
    a.label('bgot')
    a.mfspr(11, 0, 0x9250); a.addi(8, 0, 0); a.jr(9); a.nop()
    a.label('btmo')
    a.addi(8, 0, 2); a.jr(9); a.nop()
    return a.finish()


if __name__ == '__main__':
    import subprocess
    for name, blob in (('marker', marker()), ('reader', reader()),
                       ('diag_core', diag(0x11)), ('diag_periph', diag(0x9000))):
        path = f'/private/tmp/claude-501/-Users-tbelbek--claude-spares-spare-ifmGrF/3f8797bd-678d-4de2-97be-a71b1a559292/scratchpad/re/stub_{name}.bin'
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
