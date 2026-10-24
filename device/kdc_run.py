#!/usr/bin/env python3
"""Run an SRAM payload in the camera's BootROM loader via the loader's own 0xCB.

  marker  : upload stub_marker.bin (zero SPI), launch, verify it wrote 0xCAFE and returned.
  reader  : upload stub_reader.bin, read ONE flash sector at a given offset into SRAM, twice.

0xCB forms (all function=0x00102620, the ROM memory helper):
  read   : callback=0xFFFFFFFF, data-in           (proven: we read the BootROM this way)
  write  : callback=0xFFFFFFFF, data-out          (apxtool _write)
  launch : callback=ENTRY, memory_address=param   -> loader calls ENTRY(r3=param)  (apxtool)
Stops at the first anomaly. Never writes flash. macOS: run with sudo.
"""
import struct, sys, time
from pathlib import Path
import usb.core
import kdc_dump as K

HERE = Path(__file__).resolve().parent
ENTRY = 0x2000
PARAM = 0x3900


def cmd_out(bot, cdb, data, timeout=2000):
    bot.tag += 1
    cbw = struct.pack('<4sIIBBB16s', b'USBC', bot.tag, len(data), 0x00, 0, len(cdb), cdb)
    if bot.dev.write(bot.ep_out, cbw, timeout=timeout) != 31:
        raise RuntimeError('short CBW (out)')
    if bot.dev.write(bot.ep_out, data, timeout=timeout) != len(data):
        raise RuntimeError('short data (out)')
    csw = bytes(bot.dev.read(bot.ep_in, 13, timeout=timeout))
    if len(csw) != 13 or struct.unpack('<4sIIB', csw) != (b'USBS', bot.tag, 0, 0):
        raise RuntimeError(f'bad out status {csw.hex()}')


def sram_write(bot, address, data):
    for off in range(0, len(data), 256):
        part = data[off:off + 256]
        cmd_out(bot, bytes([0xCB]) + struct.pack('<III', K.ROM_FN, address + off, 0xFFFFFFFF) + b'\xff' * 3, part)


def launch(bot, entry, param, resp=24):
    cdb = bytes([0xCB]) + struct.pack('<III', K.ROM_FN, param, entry) + b'\xff' * 3
    return bot.cmd(cdb, resp)


def enter_loader():
    bot = K.stock_session()                       # verifies stock USB fingerprint
    print('sending 0xDA')
    try:
        bot.cmd(bytes([0xDA]) + b'\xff' * 15)
    except usb.core.USBError as e:
        print(f'  0xDA: no CSW ({e}) — expected')
    bot.release()
    bot, dev, ident = K.open_loader(time.monotonic() + 15)
    product = usb.util.get_string(dev, 2); maker = usb.util.get_string(dev, 1)
    if ident != ('buildwin', 'video050loader') or {maker, product} != {'BUILDWIN', 'BLDR v1.00'}:
        sys.exit('loader fingerprint differs; stop')
    print(f'in loader: {maker!r} {product!r}')
    return bot


def verify_bootrom(bot):
    # The reader executes ROM code at these sites; require them to match ONE reference
    # exactly (head[:16] is identical across refs, so pick by the actual callee bytes).
    SITES = ((0x485c, 0x4898), (0x1018, 0x1064), (0x2530, 0x2584), (0x2620, 0x265C))
    ours = {(lo, hi): bot.rom_read(0x00100000 + lo, hi - lo) for lo, hi in SITES}
    for p in K.ROM_REFS:
        r = p.read_bytes()
        if all(ours[(lo, hi)] == r[lo:hi] for lo, hi in SITES):
            print(f'BootROM reader-callees match reference {p.name}')
            return
    sys.exit('BootROM reader-callee code matches NO reference; stop (analysis may not apply)')


def marker():
    bot = enter_loader()
    stub = (HERE.parent / 'tools' / 'stub_marker.bin').read_bytes()
    sram_write(bot, ENTRY, stub)
    if bot.rom_read(ENTRY, len(stub)) != stub:
        sys.exit('stub upload readback mismatch; stop (SRAM write not working)')
    print(f'stub uploaded+verified at 0x{ENTRY:x} ({len(stub)} B)')
    sram_write(bot, PARAM, b'\0' * 16)
    launch(bot, ENTRY, PARAM)
    res = bot.rom_read(PARAM + 12, 4)
    ok = res == struct.pack('<I', 0x0000CAFE)
    print(f'marker result [0x{PARAM+12:x}] = {res.hex()}  {"OK — launch+return works" if ok else "MISMATCH"}')
    bot.release()
    print('power-cycle the camera to return to stock')


def reader(offset, length):
    assert length <= 480 and offset >= 0
    bot = enter_loader()
    verify_bootrom(bot)
    stub = (HERE.parent / 'tools' / 'stub_reader.bin').read_bytes()
    sram_write(bot, ENTRY, stub)
    if bot.rom_read(ENTRY, len(stub)) != stub:
        sys.exit('reader upload readback mismatch; stop')
    DEST = 0x3000
    def one():
        sram_write(bot, PARAM, struct.pack('<IIII', offset, length, DEST, 0))
        launch(bot, ENTRY, PARAM)
        status = struct.unpack('<I', bot.rom_read(PARAM + 12, 4))[0]
        if status != 0x600D:
            sys.exit(f'reader status 0x{status:x} != 0x600d; stop')
        return bot.rom_read(DEST, length)
    a = one(); b = one()
    print(f'two reads identical: {a == b}')
    print(f'flash@0x{offset:x} [{length}B]:\n{a.hex()}')
    bot.release()
    print('power-cycle the camera to return to stock')


def bootrom(outpath):
    bot = enter_loader()
    data = b''.join(bot.rom_read(0x00100000 + o, min(0x1000, 0x8000 - o)) for o in range(0, 0x8000, 0x1000))
    Path(outpath).write_bytes(data)
    import hashlib
    print(f'BootROM 32 KiB sha256 {hashlib.sha256(data).hexdigest()}')
    for p, sha in K.ROM_REFS.items():
        r = p.read_bytes()
        same = all(data[lo:hi] == r[lo:hi] for lo, hi in ((0, 0x8000),))
        print(f'  == {p.name}? full-32K {data == r[:0x8000]}  0x485c-match {data[0x485c:0x4898]==r[0x485c:0x4898]}')
    bot.release(); print(f'saved {outpath}; power-cycle the camera')


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'bootrom':
        bootrom(sys.argv[2] if len(sys.argv) > 2 else str(HERE / 'our_bootrom.bin'))
    elif len(sys.argv) >= 2 and sys.argv[1] == 'marker':
        marker()
    elif len(sys.argv) >= 2 and sys.argv[1] == 'reader':
        off = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0
        n = int(sys.argv[3], 0) if len(sys.argv) > 3 else 256
        reader(off, n)
    else:
        sys.exit('usage: kdc_run.py marker | reader [offset] [length]')
