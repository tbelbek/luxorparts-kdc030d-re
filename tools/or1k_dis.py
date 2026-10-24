"""Minimal little-endian OpenRISC (ORBIS32) disassembler for reading BootROM code."""
import struct, sys
def sx(v, b): return v - (1 << b) if v & (1 << (b - 1)) else v
SF = {0: 'eq', 1: 'ne', 2: 'gtu', 3: 'geu', 4: 'ltu', 5: 'leu', 10: 'gts', 11: 'ges', 12: 'lts', 13: 'les'}
ALU = {0: 'add', 2: 'sub', 3: 'and', 4: 'or', 5: 'xor', 6: 'mul', 8: 'sll/srl', 9: 'div', 10: 'divu'}
def dis(w, pc):
    op = w >> 26; d = (w >> 21) & 31; a = (w >> 16) & 31; b = (w >> 11) & 31; i16 = w & 0xffff
    if op in (0, 1, 3, 4):
        return f"{['l.j', 'l.jal', '', 'l.bnf', 'l.bf'][op]} 0x{(pc + sx(w & 0x3ffffff, 26) * 4) & 0xffffffff:08x}"
    if op == 5: return 'l.nop'
    if op == 6: return f'l.movhi r{d},0x{i16:04x}'
    if op in (0x11, 0x12): return f"{'l.jr' if op == 0x11 else 'l.jalr'} r{b}"
    loads = {0x21: 'lwz', 0x22: 'lws', 0x23: 'lbz', 0x24: 'lbs', 0x25: 'lhz', 0x26: 'lhs'}
    if op in loads: return f'l.{loads[op]} r{d},{sx(i16, 16)}(r{a})'
    imm = {0x27: 'addi', 0x29: 'andi', 0x2a: 'ori', 0x2b: 'xori', 0x2c: 'muli'}
    if op in imm: return f"l.{imm[op]} r{d},r{a},{sx(i16, 16) if op in (0x27, 0x2b, 0x2c) else hex(i16)}"
    if op == 0x2d: return f'l.mfspr r{d},r{a},0x{i16:x}'
    if op == 0x2e: return f"l.{['slli', 'srli', 'srai', 'rori'][(w >> 6) & 3]} r{d},r{a},{w & 63}"
    if op == 0x2f: return f'l.sf{SF.get(d, d)}i r{a},{sx(i16, 16)}'
    if op == 0x30: return f'l.mtspr r{a},r{b},0x{((d << 11) | (w & 0x7ff)):x}'
    stores = {0x35: 'sw', 0x36: 'sb', 0x37: 'sh'}
    if op in stores: return f'l.{stores[op]} {sx((d << 11) | (w & 0x7ff), 16)}(r{a}),r{b}'
    if op == 0x38:
        sub = w & 0xf
        if sub == 8: return f"l.{['sll', 'srl', 'sra', 'ror'][(w >> 6) & 3]} r{d},r{a},r{b}"
        return f'l.{ALU.get(sub, f"alu{sub}")} r{d},r{a},r{b}'
    if op == 0x39: return f'l.sf{SF.get(d, d)} r{a},r{b}'
    return f'.word 0x{w:08x} (op 0x{op:x})'
if __name__ == '__main__':
    data = open(sys.argv[1], 'rb').read(); base = int(sys.argv[2], 16)
    start, end = int(sys.argv[3], 16), int(sys.argv[4], 16)
    for off in range(start, end, 4):
        w = struct.unpack_from('<I', data, off)[0]
        print(f'{base + off:08x}: {w:08x}  {dis(w, base + off)}')
