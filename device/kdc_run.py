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
try: sys.stdout.reconfigure(line_buffering=True)
except Exception: pass

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


def launch(bot, entry, param, resp=24, timeout=30000):
    # The 0xCB handler calls our stub and only replies once it RETURNS, so wait long
    # enough for the stub to finish (earlier 1500ms default timed out the host before
    # the stub's SPI polling completed — mistaken for "USB died").
    cdb = bytes([0xCB]) + struct.pack('<III', K.ROM_FN, param, entry) + b'\xff' * 3
    return bot.cmd(cdb, resp, timeout=timeout)


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
    stub = (HERE.parent / 're/stub_marker.bin').read_bytes()
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
    stub = (HERE.parent / 're/stub_reader.bin').read_bytes()
    sram_write(bot, ENTRY, stub)
    if bot.rom_read(ENTRY, len(stub)) != stub:
        sys.exit('reader upload readback mismatch; stop')
    # Put the data buffer INSIDE the param block (dest = PARAM+20) and read everything
    # back from launch()'s single response — the 0xCB handler returns `resp` bytes from
    # PARAM after the stub runs, so status+jedec+data come in ONE transaction. Avoids a
    # second 0xCB read, which fails after the stub has driven SPI.
    # The stub restores all SPRs before returning, so the device is pristine and SEPARATE
    # 0xCB reads of our SRAM result work (as the marker proved). 30s launch lets it finish.
    DEST = 0x3000
    def one():
        sram_write(bot, PARAM, struct.pack('<IIII', offset, length, DEST, 0))
        launch(bot, ENTRY, PARAM)
        print('  stub returned (launch ok)')
        status = struct.unpack('<I', bot.rom_read(PARAM + 12, 4))[0]
        jedec = bot.rom_read(PARAM + 16, 3)
        print(f'  status 0x{status:08x}  JEDEC {jedec.hex()} (expect 5e4016)')
        if status != 0x600D:
            print('  reader error (status != 0x600d); not a brick — power-cycle returns to stock')
            return None
        return bot.rom_read(DEST, length)
    a = one()
    if a is None:
        print('read did not complete; see status/JEDEC above')
    else:
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


def flasher(offset, length):
    # Use matu's PROVEN flasher payload (boot/flasher.c -> re/flasher.bin): its own SPI driver,
    # no vendor addresses, runs in the 0xDA loader. Request block layout:
    #   magic(0)=0x52585041, sequence(4), command(8), address(12), length(16), status(20), flags(24), data(28)
    # command 1 = flash read (addr<0x200000, len<=512 for SRAM). Retrieve via separate read (SRAM uncached).
    REQ = 0x3400                       # request block; flasher.S puts stack at REQ-0x110, save at REQ-0x100
    assert length <= 512
    blob = (HERE.parent / 're' / 'flasher.bin').read_bytes()
    bot = enter_loader()
    sram_write(bot, ENTRY, blob)
    if bot.rom_read(ENTRY, len(blob)) != blob:
        sys.exit('flasher upload readback mismatch; stop')
    print(f'flasher.bin uploaded at 0x{ENTRY:x} ({len(blob)} B)')
    def cmd(command, addr, n, payload=b''):
        hdr = struct.pack('<7I', 0x52585041, 1, command, addr, n, 1, 0)
        sram_write(bot, REQ, hdr + payload + b'\0' * (n - len(payload) if command != 1 else n))
        launch(bot, ENTRY, REQ)        # callback=ENTRY, memory_address=REQ -> rescue(r3=REQ)
        status = struct.unpack('<I', bot.rom_read(REQ + 20, 4))[0]
        data = bot.rom_read(REQ + 28, n) if n else b''
        return status, data
    # command 0: init SPI + JEDEC (addr=0 -> full init); returns the JEDEC id in data[0:3]
    st, jd = cmd(0, 0, 3)
    print(f'  cmd0 (init+JEDEC): status 0x{st:08x}  JEDEC {jd.hex()} (chip 5e4016; flasher expects 5e4015 so status 3 is OK)')
    # command 1: read flash
    st, data = cmd(1, offset, length)
    print(f'  cmd1 (read): status 0x{st:08x}')
    if st == 0:
        print(f'flash@0x{offset:x} [{length}B]:\n{data.hex()}')
    else:
        print('  read did not complete cleanly; data may be partial:')
        print(data.hex())
    bot.release()


def dumpflash(start, total, outpath):
    # Loop the proven flasher cmd1 in 256-byte chunks over [start, start+total) and save to a file.
    REQ = 0x3400
    blob = (HERE.parent / 're' / 'flasher.bin').read_bytes()
    bot = enter_loader()
    sram_write(bot, ENTRY, blob)
    if bot.rom_read(ENTRY, len(blob)) != blob:
        sys.exit('flasher upload readback mismatch; stop')
    def cmd(command, addr, n):
        sram_write(bot, REQ, struct.pack('<7I', 0x52585041, 1, command, addr, n, 1, 0) + b'\0' * n)
        launch(bot, ENTRY, REQ)
        status = struct.unpack('<I', bot.rom_read(REQ + 20, 4))[0]
        return status, (bot.rom_read(REQ + 28, n) if n else b'')
    st, jd = cmd(0, 0, 3)
    print(f'  cmd0 init+JEDEC: status 0x{st:x} JEDEC {jd.hex()}')
    out = bytearray(); off = start
    while off < start + total:
        n = min(256, start + total - off)
        st, d = cmd(1, off, n)
        if st != 0 or len(d) != n:
            print(f'  stop at 0x{off:x}: status 0x{st:x} len {len(d)}'); break
        out += d; off += n
        if (off - start) % 0x1000 == 0:
            print(f'  {off - start:#x}/{total:#x}', flush=True)
    Path(outpath).write_bytes(out)
    print(f'saved {len(out)} bytes of flash 0x{start:x}.. to {outpath}')
    bot.release()


def diag(which):
    # Diagnostic ladder: does reading an SPR in the loader fault? Upload a minimal stub that
    # reads one SPR and returns; retrieve stage+value via SEPARATE reads (marker-proven path).
    stub = (HERE.parent / 're' / f'stub_diag_{which}.bin').read_bytes()
    bot = enter_loader()
    sram_write(bot, ENTRY, stub)
    if bot.rom_read(ENTRY, len(stub)) != stub:
        sys.exit('diag stub upload readback mismatch; stop')
    sram_write(bot, PARAM, b'\0' * 24)
    launch(bot, ENTRY, PARAM)
    print(f'  diag_{which}: stub returned (launch ok)')
    stage = struct.unpack('<I', bot.rom_read(PARAM + 12, 4))[0]
    value = struct.unpack('<I', bot.rom_read(PARAM + 16, 4))[0]
    done = 'DONE (SPR read did NOT fault)' if stage == 0x0000D04E else f'stopped at stage 0x{stage:08x}'
    print(f'  stage: 0x{stage:08x} -> {done}')
    print(f'  SPR value read: 0x{value:08x}')
    bot.release()


def stockread(address, length):
    # NON-SPI path: in STOCK mode (DRAM up, no loader), the stock firmware's 0xCD vendor
    # command does a pure device-to-host memory read when callback=0xFFFFFFFF (it sends
    # memory, executes no code of ours). Reads the running application from RAM 0x02000000+.
    # The ONE risk: the handler jalr's the wrapper 0x0204109c; if our app build differs from
    # penguin's that address is wrong (-> crash, recoverable; low brick risk). Tiny read first.
    WRAPPER = 0x0204109C
    bot = K.stock_session()        # verifies stock USB fingerprint; stays in stock mode (NO 0xDA)
    def cd_read(addr, n):
        # Raw BOT: send CBW, read the data phase, then tolerate a missing/late CSW so we still
        # see the bytes if the wrapper sent data but didn't close the handshake cleanly.
        bot.tag += 1
        cdb = bytes([0xCD]) + struct.pack('<III', WRAPPER, addr, 0xFFFFFFFF) + b'\x00' * 3
        cbw = struct.pack('<4sIIBBB16s', b'USBC', bot.tag, n, 0x80, 0, len(cdb), cdb)
        bot.dev.write(bot.ep_out, cbw, timeout=3000)
        try:
            data = bytes(bot.dev.read(bot.ep_in, n, timeout=5000))
        except usb.core.USBError as e:
            return None, f'data-phase timeout ({e})'
        try:
            csw = bytes(bot.dev.read(bot.ep_in, 13, timeout=2000))
            csw = 'ok' if csw[:4] == b'USBS' else csw.hex()
        except usb.core.USBError:
            csw = '(no CSW — handshake not closed, data may still be valid)'
        return data, csw
    print(f'0xCD stock read @0x{address:x} [{length}B], wrapper 0x{WRAPPER:x}')
    data, csw = cd_read(address, min(length, 256))
    print(f'  CSW: {csw}')
    print(f'  data: {data.hex() if data else "(none)"}')
    bot.release()


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'dump':
        dumpflash(int(sys.argv[2], 0), int(sys.argv[3], 0),
                  sys.argv[4] if len(sys.argv) > 4 else str(HERE / 'flashdump.bin'))
    elif len(sys.argv) >= 2 and sys.argv[1] == 'flasher':
        flasher(int(sys.argv[2], 0) if len(sys.argv) > 2 else 0,
                int(sys.argv[3], 0) if len(sys.argv) > 3 else 256)
    elif len(sys.argv) >= 2 and sys.argv[1] == 'diag':
        diag(sys.argv[2] if len(sys.argv) > 2 else 'core')
    elif len(sys.argv) >= 2 and sys.argv[1] == 'stockread':
        stockread(int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x02000000,
                  int(sys.argv[3], 0) if len(sys.argv) > 3 else 16)
    elif len(sys.argv) >= 2 and sys.argv[1] == 'bootrom':
        bootrom(sys.argv[2] if len(sys.argv) > 2 else str(HERE / 'our_bootrom.bin'))
    elif len(sys.argv) >= 2 and sys.argv[1] == 'marker':
        marker()
    elif len(sys.argv) >= 2 and sys.argv[1] == 'reader':
        off = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0
        n = int(sys.argv[3], 0) if len(sys.argv) > 3 else 256
        reader(off, n)
    else:
        sys.exit('usage: kdc_run.py stockread [addr] [len] | marker | reader [offset] [length] | bootrom')
