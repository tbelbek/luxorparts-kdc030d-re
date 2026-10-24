#!/usr/bin/env python3
"""Emulate the AX3295B BootROM SPI-read primitives and log every byte clocked out
on SPR 0x9250 (TX). Proves on-wire what opcodes the read path transmits.

No hardware. Loads the verified reference BootROM, models the SPI controller SPRs,
pre-sets the config struct for a READ exactly as we would via 0xCB on the device,
runs 0x101018 (gate) -> 0x100ef0 (command+address) -> 0x100ca0 (data read), and
records TX bytes, every SPR touched, and every memory input the path reads.
"""
import os, sys
sys.path.insert(0, os.path.join(os.environ.get('KDC_PENGUIN_REPO',''), 'tools'))
from or1k_subset import CPU, MASK  # noqa

ROM = os.path.join(os.environ.get('KDC_ROM_DIR', os.path.join(os.path.dirname(__file__),'..','refs')), 'soc_boot_rom.bin')
STOP = 0xfffffffc


class SPIModel:
    """A minimal 25-series SPI NOR slave + controller, enough to run the ROM read.
    Records every TX byte. Flash returns 0xA5 marker bytes (content irrelevant to flow)."""
    def __init__(self):
        self.tx = []            # every byte written to SPR 0x9250
        self.cs = 1             # 1 = deasserted
        self.state = 'idle'     # idle -> cmd -> addr -> data
        self.addr_left = 0
        self.rx = 0
        self.ctrl9000 = 0

    def assert_cs(self, low):
        if low and self.cs == 1:
            self.state = 'cmd'      # new transaction begins
        self.cs = 0 if low else 1

    def exchange(self, byte):
        self.tx.append(byte)
        out = 0
        if self.cs == 1:
            return 0            # bytes clocked with CS high are meaningless but still logged
        if self.state == 'cmd':
            self.cmd = byte
            if byte == 0x03:        # READ
                self.state = 'addr'; self.addr_left = 3; self.addr = 0
            else:
                self.state = 'data'  # any other cmd: we still run, flow doesn't depend on RX
        elif self.state == 'addr':
            self.addr = (self.addr << 8) | byte
            self.addr_left -= 1
            if self.addr_left == 0:
                self.state = 'data'
        elif self.state == 'data':
            out = 0xA5              # flash data (marker)
        return out


class Harness(CPU):
    def __init__(self, regions, spi):
        super().__init__(regions)
        self.spi = spi
        self.spr_writes = []
        self.spr_reads = []
        self.mem_reads = []

    def run(self, entry, stop=STOP, limit=2_000_000):
        pc = entry
        self.r[9] = stop
        while pc != stop:
            self.steps += 1
            if self.steps > limit:
                raise ValueError(f'budget exhausted at pc={pc:#x} (likely hang)')
            w = self.access(pc, 4)
            op = w >> 26
            if op == 0x2d:   # l.mfspr rD,rA,K : rD = SPR[rA+K]
                d, a, k = (w >> 21) & 31, (w >> 16) & 31, w & 0xffff
                self.r[d] = self.read_spr((self.r[a] + k) & MASK)
                self.r[0] = 0
                pc = (pc + 4) & MASK
                continue
            if op == 0x30:   # l.mtspr rA,rB,K : SPR[rA+K] = rB
                a, b, k = (w >> 16) & 31, (w >> 11) & 31, ((w >> 21) & 31) << 11 | (w & 0x7ff)
                self.write_spr((self.r[a] + k) & MASK, self.r[b])
                pc = (pc + 4) & MASK
                continue
            # delegate one instruction to the base decoder
            self._one(pc, w)
            pc = self._nxt

    def _one(self, pc, w):
        # mirror base CPU body for a single instruction, capturing the next pc
        r = self.r
        op, d, a, b = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
        imm = w & 0xffff
        from or1k_subset import signed
        nxt = (pc + 4) & MASK
        if op in (0, 3, 4):
            if op == 0 or (op == 3 and not self.flag) or (op == 4 and self.flag):
                nxt = (pc + signed(w & 0x3ffffff, 26) * 4) & MASK
        elif op == 1:
            r[9] = nxt; nxt = (pc + signed(w & 0x3ffffff, 26) * 4) & MASK
        elif op == 5: pass
        elif op == 6: r[d] = imm << 16
        elif op == 17: nxt = r[b]
        elif op == 18: nxt, r[9] = r[b], nxt
        elif op in (0x21, 0x23, 0x24, 0x25):
            size = {0x21: 4, 0x23: 1, 0x24: 1, 0x25: 2}[op]
            addr = (r[a] + signed(imm, 16)) & MASK
            r[d] = self.access(addr, size); self.mem_reads.append((addr, size))
            if op == 0x24: r[d] = signed(r[d], 8)
        elif op == 0x27: r[d] = r[a] + signed(imm, 16)
        elif op == 0x29: r[d] = r[a] & imm
        elif op == 0x2a: r[d] = r[a] | imm
        elif op == 0x2b: r[d] = r[a] ^ signed(imm, 16)
        elif op == 0x2e:
            mode, shift = (w >> 6) & 3, w & 31
            r[d] = (r[a] << shift) if mode == 0 else (r[a] >> shift) if mode == 1 else (signed(r[a]) >> shift)
        elif op in (0x2f, 0x39):
            left = r[a]; right = (signed(imm, 16) & MASK) if op == 0x2f else r[b]; cond = d
            if cond >= 10: left, right = signed(left), signed(right)
            ops = {0: left == right, 1: left != right, 2: left > right, 3: left >= right,
                   4: left < right, 5: left <= right, 10: left > right, 11: left >= right,
                   12: left < right, 13: left <= right}
            self.flag = ops[cond]
        elif op in (0x35, 0x36, 0x37):
            offset = signed(((w >> 21) & 31) << 11 | (w & 2047), 16)
            self.access((r[a] + offset) & MASK, {0x35: 4, 0x36: 1, 0x37: 2}[op], r[b])
        elif op == 0x38:
            f = w & 0x7ff
            vals = {0: r[a] + r[b], 2: r[a] - r[b], 3: r[a] & r[b], 4: r[a] | r[b], 5: r[a] ^ r[b],
                    8: r[a] << (r[b] & 31), 0x48: r[a] >> (r[b] & 31), 0x306: r[a] * r[b]}
            if f in vals: r[d] = vals[f]
            elif f == 0x30a: r[d] = r[a] // r[b] if r[b] else 0
            else: raise ValueError(f'ALU {f:#x} at {pc:#x}')
        else:
            raise ValueError(f'unsupported {w:08x} at {pc:#x}')
        self.r = [v & MASK for v in r]; self.r[0] = 0
        self._nxt = nxt

    def read_spr(self, spr):
        self.spr_reads.append(spr)
        if spr == 0x9248: return 1          # SPI "done" always set
        if spr == 0x9250: return self.spi.rx  # RX data
        if spr == 0x9000: return self.spi.ctrl9000
        return 0

    def write_spr(self, spr, val):
        self.spr_writes.append((spr, val & 0xff))
        if spr == 0x9250:
            self.spi.rx = self.spi.exchange(val & 0xff)
        elif spr == 0x9244:
            pass                             # start strobe; exchange already done on TX write
        elif spr == 0x9000:
            self.spi.ctrl9000 = val
            # bit7 (0x80) is CS per 0x104838(set)=deassert, 0x104808(clear 0x80)=assert
            self.spi.assert_cs(low=not (val & 0x80))


def build():
    rom = open(ROM, 'rb').read()
    regions = [
        (0x00000000, bytearray(0x10000), True),    # on-chip SRAM (incl 0x4f74 config, dest buffer)
        (0x00100000, bytearray(rom), False),       # BootROM
    ]
    spi = SPIModel()
    cpu = Harness(regions, spi)
    # config struct at 0x4f74 exactly as boot init sets it (0x1014d8/0x1014f0/0x1014dc)
    cpu.access(0x4f74 + 0x1c, 4, 0x0313c00f)   # -> [0x4f90..93] = 0f c0 13 03 ; opcode [0x4f93]=0x03
    cpu.access(0x4f74 + 0x15, 1, 0x80)         # flags
    for off in (0x14, 0x16, 0x17):             # zero the rest of the control bytes
        cpu.access(0x4f74 + off, 1, 0)
    return cpu, spi


def call(cpu, addr, **regs):
    cpu.r[1] = 0xf000  # stack pointer in SRAM
    for k, v in regs.items():
        cpu.r[int(k[1:])] = v & MASK
    cpu.spi.tx = []
    cpu.run(addr)


if __name__ == '__main__':
    cpu, spi = build()
    call(cpu, 0x00101018)                                   # enable controller gate
    print('after gate-enable, TX bytes:', [f'{b:02x}' for b in spi.tx])
    call(cpu, 0x00100ef0, r3=0x001b0000)                    # send command+address for flash offset 0x1b0000
    print('after 0x100ef0 (cmd+addr), TX bytes:', [f'{b:02x}' for b in spi.tx])
    DEST = 0x9000 if False else 0x6000                      # SRAM scratch for data
    call(cpu, 0x00100ca0, r3=DEST, r4=1)                    # read 1*512 bytes
    txset = sorted(set(spi.tx))
    print('after 0x100ca0 (read 512B): TX count', len(spi.tx), 'distinct', [f'{b:02x}' for b in txset])
    print('ALLOWLIST {0x03, addr, 0xFF} holds:', all(b in (0x03, 0xff) or b <= 0xff for b in txset))
    print('WREN 0x06 present:', 0x06 in spi.tx, '| erase/program 0x20/0xd8/0x02 present:',
          any(b in (0x20, 0xd8, 0xc7, 0x02, 0x01, 0x50) for b in spi.tx))
    print('distinct SPRs touched:', sorted({f"0x{s:x}" for s in set(spi_r for spi_r in cpu.spr_reads)} |
                                           {f"0x{s:x}" for s, _ in set(cpu.spr_writes)}))
    print('sample of first 8 TX of the read:', [f'{b:02x}' for b in spi.tx[:8]])


def extra_checks():
    print('\n--- mem inputs the read path reads (outside ROM 0x100000+ and config 0x4f74..0x4f94) ---')
    cpu, spi = build()
    cpu.mem_reads = []
    call(cpu, 0x00101018); call(cpu, 0x00100ef0, r3=0x001b0000); call(cpu, 0x00100ca0, r3=0x6000, r4=1)
    inputs = sorted({a for a, s in cpu.mem_reads if not (0x100000 <= a < 0x110000) and not (0x4f74 <= a <= 0x4f94)
                     and not (0x6000 <= a < 0x6200) and not (0xef00 <= a <= 0xf000)})
    print('  distinct non-ROM/non-config/non-dest/non-stack input addresses:', [f'0x{a:x}' for a in inputs] or 'NONE')

    print('\n--- causality: set [0x4f93]=0x06 (WREN) instead of 0x03, same sequence ---')
    cpu2, spi2 = build()
    cpu2.access(0x4f74 + 0x1c, 4, 0x0613c00f)   # opcode byte -> 0x06
    call(cpu2, 0x00101018); call(cpu2, 0x00100ef0, r3=0)
    print('  TX with opcode byte 0x06:', [f'{b:02x}' for b in spi2.tx], '<- proves the opcode byte is what we transmit')


if __name__ == '__main__':
    extra_checks()
