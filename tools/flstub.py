#!/usr/bin/env python3
"""Faithful hand-port of matu boot/flasher.c (commands 0 and 1 only) to little-endian
OpenRISC, using the Asm from stub_build. Entry matches flasher.S (r3 = request block;
save area at r3-0x100, stack at r3-0x110). Request layout:
  magic(0)=0x52585041, sequence(4), command(8), address(12), length(16), status(20), flags(24), data(28)
No cache flush (SRAM window is uncached, so flasher.c's sync() is a no-op there).
Only {0x9f,0x03,0xFF} and address bytes ever go to the SPI TX; no WREN/erase/program.
"""
import struct, sys
sys.path.insert(0, '/private/tmp/claude-501/-Users-tbelbek--claude-spares-spare-ifmGrF/3f8797bd-678d-4de2-97be-a71b1a559292/scratchpad/re')
from stub_build import Asm

MAGIC = 0x52585041
INIT_UPD = [(0x8850, 0xf0000000, 0x30000000), (0x8844, 0x8, 0), (0x8840, 0x100000, 0),
            (0x8344, 0x300000, 0x100000), (0x8100, 0xd00, 0xd00), (0x8040, 0xd00, 0), (0x8000, 0x400, 0x400)]
INIT_WR = ([(0x9000, 0x01115005), (0x9004, 0), (0x9008, 0), (0x900c, 0),
            (0x9244, 0x03ff0000), (0x9248, 0), (0x924c, 5)]
           + [(r, 0) for r in range(0x9250, 0x9280, 4)]
           + [(0x9260, 0xffff), (0x9264, 0xffffffff), (0x926c, 0x0fffffff), (0x9270, 0x0fffffff)])


def build(base=0x2000):
    a = Asm(base)
    # --- entry (flasher.S): r3 = request block ---
    a.addi(11, 3, -0x100); a.sw(11, 0, 1); a.sw(11, 4, 2); a.sw(11, 8, 9); a.sw(11, 12, 10)
    # also save the callee-saved regs this port clobbers, so the BootROM loader is unharmed
    a.sw(11, 16, 14); a.sw(11, 20, 16); a.sw(11, 24, 18); a.sw(11, 28, 20)
    a.addi(1, 3, -0x110)                 # stack
    a.ori(16, 3, 0)                      # r16 = request block (p)
    # watchdog off (flags bit0 = KEEP_WDT; we never set it): wr(0x880c,0x5500)
    a.const(2, 0x5500); a.mtspr(0, 2, 0x880c)
    # magic check: if [p+0] != MAGIC -> return with status left = 1
    a.const(2, 0x00000001); a.sw(16, 20, 2)          # status = 1 (not done) up front
    a.lwz(5, 16, 0); a.const(6, MAGIC); a.sfne(5, 6); a.bf('ret')   # if magic != MAGIC: ret
    a.nop()
    a.lwz(17, 16, 8)                     # r17 = command
    a.lwz(18, 16, 12)                    # r18 = address
    a.lwz(19, 16, 16)                    # r19 = length
    # dispatch: command 0 -> init+jedec ; command 1 -> read ; else ret
    a.sfeqi(17, 0); a.bf('cmd0'); a.nop()
    a.sfeqi(17, 1); a.bf('cmd1'); a.nop()
    a.j('ret'); a.nop()

    # --- cmd0: full SPI init (address bit0==0) then JEDEC 0x9f -> data[0:3] ---
    a.label('cmd0')
    for spr, m, v in INIT_UPD: a.updspr(spr, m, v)
    for spr, v in INIT_WR: a.wrspr(spr, v)
    a.updspr(0x9000,0x4000,0x4000); a.updspr(0x9000,0x4000,0)               # cs(1); cs(0)
    a.addi(3, 0, 0x9f); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
    a.addi(20, 0, 0)                     # i=0
    a.label('j0'); a.sfgeui(20, 3); a.bf('j0e'); a.nop()
    a.addi(3, 0, 0xff); a.addi(4, 0, 0x80); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
    a.add(6, 16, 20); a.sb(6, 28, 11)    # data[i] = RX  ([p+28+i])
    a.addi(20, 20, 1); a.j('j0'); a.nop()
    a.label('j0e'); a.updspr(0x9000,0x4000,0x4000); a.j('done'); a.nop()

    # --- cmd1: read: cs(0); send 0x03 + 3 addr bytes; data[i]=byte(0xff); cs(1) ---
    a.label('cmd1')
    a.updspr(0x9000,0x4000,0)
    a.addi(3, 0, 0x03); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
    a.srli(3, 18, 16); a.andi(3, 3, 0xff); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
    a.srli(3, 18, 8); a.andi(3, 3, 0xff); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
    a.ori(3, 18, 0); a.andi(3, 3, 0xff); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
    a.addi(20, 0, 0)                     # i=0
    a.label('rl'); a.sfgeu(20, 19); a.bf('rle'); a.nop()
    a.addi(3, 0, 0xff); a.addi(4, 0, 0x80); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
    a.add(6, 16, 20); a.sb(6, 28, 11)    # data[i] = RX
    a.addi(20, 20, 1); a.j('rl'); a.nop()
    a.label('rle'); a.updspr(0x9000,0x4000,0x4000)

    # --- done: deassert CS (in case of a timeout mid-transfer), status = r8, return ---
    a.label('done')
    a.updspr(0x9000, 0x4000, 0x4000)     # ensure CS high even on an error exit
    a.sw(16, 20, 8)                      # status = r8
    a.label('ret')
    a.addi(11, 1, 0x10); a.lwz(1, 11, 0); a.lwz(2, 11, 4); a.lwz(9, 11, 8); a.lwz(10, 11, 12)
    a.lwz(14, 11, 16); a.lwz(16, 11, 20); a.lwz(18, 11, 24); a.lwz(20, 11, 28)   # restore callee-saved
    a.jr(9); a.nop()

    # --- byte(r3=value, r4=recv-bits) -> r11=RX, r8=0/2 ; feeds watchdog in poll ---
    a.label('byte')
    a.mfspr(5, 0, 0x9000); a.const(2, 0xFFFFFF7F); a.and_(5, 5, 2); a.or_(5, 5, 4); a.mtspr(0, 5, 0x9000)
    a.mtspr(0, 3, 0x9250); a.addi(6, 0, 1); a.mtspr(0, 6, 0x9244)
    a.const(7, 0x00080000)
    a.label('bp')
    a.mfspr(2, 0, 0x880c); a.sfeqi(2, 0); a.bf('bnf'); a.nop()
    a.const(2, 0x0005001d); a.mtspr(0, 2, 0x880c)
    a.label('bnf')
    a.mfspr(5, 0, 0x9248); a.andi(5, 5, 1); a.sfnei(5, 0); a.bf('bg'); a.nop()
    a.addi(7, 7, -1); a.sfeqi(7, 0); a.bf('bt'); a.nop()
    a.j('bp'); a.nop()
    a.label('bg'); a.mfspr(11, 0, 0x9250); a.addi(8, 0, 0); a.jr(9); a.nop()
    a.label('bt'); a.addi(8, 0, 2); a.jr(9); a.nop()
    return a.finish()


if __name__ == '__main__':
    blob = build()
    out = '/private/tmp/claude-501/-Users-tbelbek--claude-spares-spare-ifmGrF/3f8797bd-678d-4de2-97be-a71b1a559292/scratchpad/re/flasher.bin'
    open(out, 'wb').write(blob)
    print(f'flstub -> {len(blob)} bytes @ {out}')
    tx = sorted({struct.unpack_from('<I', blob, i)[0] & 0xffff for i in range(0, len(blob), 4)
                 if (struct.unpack_from('<I', blob, i)[0] >> 26) == 0x27
                 and ((struct.unpack_from('<I', blob, i)[0] >> 21) & 31) == 3
                 and ((struct.unpack_from('<I', blob, i)[0] >> 16) & 31) == 0})
    print('TX constants:', [hex(x) for x in tx], 'dangerous?', bool(set(tx) & {0x06, 0x20, 0xd8, 0xc7, 0x02, 0x01, 0x50}))
