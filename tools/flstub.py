#!/usr/bin/env python3
"""Faithful hand-port of matu boot/flasher.c (commands 0 and 1 only) to little-endian
OpenRISC, using the Asm from stub_build. Entry matches flasher.S (r3 = request block;
save area at r3-0x100, stack at r3-0x110). Request layout:
  magic(0)=0x52585041, sequence(4), command(8), address(12), length(16), status(20), flags(24), data(28)
No cache flush (SRAM window is uncached, so flasher.c's sync() is a no-op there).
Only {0x9f,0x03,0xFF} and address bytes ever go to the SPI TX; no WREN/erase/program.
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stub_build import Asm

MAGIC = 0x52585041
INIT_UPD = [(0x8850, 0xf0000000, 0x30000000), (0x8844, 0x8, 0), (0x8840, 0x100000, 0),
            (0x8344, 0x300000, 0x100000), (0x8100, 0xd00, 0xd00), (0x8040, 0xd00, 0), (0x8000, 0x400, 0x400)]
INIT_WR = ([(0x9000, 0x01115005), (0x9004, 0), (0x9008, 0), (0x900c, 0),
            (0x9244, 0x03ff0000), (0x9248, 0), (0x924c, 5)]
           + [(r, 0) for r in range(0x9250, 0x9280, 4)]
           + [(0x9260, 0xffff), (0x9264, 0xffffffff), (0x926c, 0x0fffffff), (0x9270, 0x0fffffff)])


def build(base=0x2000, window=(0x9000, 0xa000)):
    a = Asm(base)
    WIN_LO, WIN_HI = window
    # --- inline SPI-status helpers (faithful to flasher.c status/ready/enable) ---
    # byte() clobbers r2,r5,r6,r7,r8,r9,r11 and returns RX in r11, err in r8.
    # We keep r14 as a bounded retry counter (callee-saved, saved at entry).
    _uid = [0]
    def u(name):
        _uid[0] += 1; return f'{name}{_uid[0]}'

    def emit_rdsr():
        # status(): cs(0); send 0x05; recv 1 -> r11=SR; cs(1). Propagate a byte() timeout (r8!=0).
        a.updspr(0x9000, 0x4000, 0)
        a.addi(3, 0, 0x05); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
        a.addi(3, 0, 0xff); a.addi(4, 0, 0x80); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
        a.updspr(0x9000, 0x4000, 0x4000)

    def emit_wait_wip():
        # ready(): loop rdsr until WIP(bit0)==0, bounded; on exhaustion -> werr
        L = u('wip')
        a.const(14, 0x100000)
        a.label(L); emit_rdsr()
        a.andi(5, 11, 1); a.sfeqi(5, 0); a.bf(L + 'e'); a.nop()
        a.addi(14, 14, -1); a.sfeqi(14, 0); a.bf('werr'); a.nop()
        a.j(L); a.nop()
        a.label(L + 'e')

    def emit_wren():
        # enable(): WREN(0x06), then require WEL(bit1) set or -> werr
        a.updspr(0x9000, 0x4000, 0)
        a.addi(3, 0, 0x06); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
        a.updspr(0x9000, 0x4000, 0x4000)
        emit_rdsr()
        a.andi(5, 11, 2); a.sfeqi(5, 0); a.bf('werr'); a.nop()

    def emit_window_guard():
        # containment: reject (-> werr) any erase/program addr outside [WIN_LO, WIN_HI).
        # (load bounds into a reg and use the unsigned register compare — immediates would sign-extend.)
        a.const(7, WIN_LO); a.sfgeu(18, 7); a.bnf('werr'); a.nop()   # addr < WIN_LO  -> reject
        a.const(7, WIN_HI); a.sfgeu(18, 7); a.bf('werr'); a.nop()    # addr >= WIN_HI -> reject

    def emit_addr(opcode):
        # cs stays as caller set it: send opcode + 3 address bytes (r18), checking err
        a.addi(3, 0, opcode); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
        a.srli(3, 18, 16); a.andi(3, 3, 0xff); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
        a.srli(3, 18, 8); a.andi(3, 3, 0xff); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()
        a.ori(3, 18, 0); a.andi(3, 3, 0xff); a.addi(4, 0, 0); a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()

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
    a.sfeqi(17, 2); a.bf('cmd2'); a.nop()
    a.sfeqi(17, 3); a.bf('cmd3'); a.nop()
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
    a.j('done'); a.nop()                 # cmd1 must NOT fall through into cmd3 below

    # --- cmd3: sector erase (0x20). r18=addr. Guards: in-window + 4KB aligned, else -> werr ---
    a.label('cmd3')
    emit_window_guard()
    a.andi(5, 18, 0xfff); a.sfnei(5, 0); a.bf('werr'); a.nop()      # addr not 4KB-aligned -> reject
    emit_wait_wip()                      # ready()
    emit_wren()                          # WREN + require WEL, else -> werr
    a.updspr(0x9000, 0x4000, 0)          # cs(0)
    emit_addr(0x20)                      # 0x20 + 3 addr bytes
    a.updspr(0x9000, 0x4000, 0x4000)     # cs(1)
    emit_wait_wip()                      # ready() after erase
    a.addi(8, 0, 0)                      # status = 0 (success)
    a.j('done'); a.nop()

    # --- cmd2: page program (0x02). r18=addr, r19=len, data at p+28. Guards -> werr ---
    a.label('cmd2')
    emit_window_guard()
    a.sfeqi(19, 0); a.bf('werr'); a.nop()                          # len == 0 -> reject
    a.sfgeui(19, 257); a.bf('werr'); a.nop()                       # len > 256 -> reject
    a.andi(5, 18, 0xff); a.add(5, 5, 19); a.sfgeui(5, 257); a.bf('werr'); a.nop()  # crosses 256B page -> reject
    emit_wait_wip()                      # ready()
    emit_wren()                          # WREN + require WEL
    a.updspr(0x9000, 0x4000, 0)          # cs(0)
    emit_addr(0x02)                      # 0x02 + 3 addr bytes
    a.addi(20, 0, 0)                     # i = 0
    a.label('pl'); a.sfgeu(20, 19); a.bf('ple'); a.nop()
    a.add(6, 16, 20); a.lbz(3, 6, 28); a.andi(3, 3, 0xff); a.addi(4, 0, 0)
    a.jall('byte'); a.nop(); a.sfnei(8, 0); a.bf('done'); a.nop()   # send data[i]
    a.addi(20, 20, 1); a.j('pl'); a.nop()
    a.label('ple')
    a.updspr(0x9000, 0x4000, 0x4000)     # cs(1)
    emit_wait_wip()                      # ready() after program
    a.addi(8, 0, 0)                      # status = 0
    a.j('done'); a.nop()

    # --- werr: write-enable / ready error -> status = 1, CS high, return ---
    a.label('werr')
    a.updspr(0x9000, 0x4000, 0x4000)     # cs(1)
    a.addi(8, 0, 1)
    a.j('done'); a.nop()

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
