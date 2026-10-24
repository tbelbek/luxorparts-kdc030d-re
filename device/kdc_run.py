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
sys.path.insert(0, str(HERE.parent / 'tools'))
import flstub                                   # host builds window-guarded payloads in-memory
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
    blob = flstub.build()
    bot = enter_loader()
    sram_write(bot, ENTRY, blob)
    if bot.rom_read(ENTRY, len(blob)) != blob:
        sys.exit('flasher upload readback mismatch; stop')
    print(f'flasher payload uploaded at 0x{ENTRY:x} ({len(blob)} B)')
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
    blob = flstub.build()
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
    if st != 0 or jd != bytes([0x5e, 0x40, 0x16]):
        bot.release(); sys.exit(f'cmd0 init/JEDEC failed (status 0x{st:x}, jedec {jd.hex()}); aborting dump')
    out = bytearray(); off = start; failed = False
    while off < start + total:
        n = min(480, start + total - off)
        st, d = cmd(1, off, n)
        if st != 0 or len(d) != n:
            print(f'  stop at 0x{off:x}: status 0x{st:x} len {len(d)}'); failed = True; break
        out += d; off += n
        if (off - start) % 0x1000 == 0:
            print(f'  {off - start:#x}/{total:#x}', flush=True)
    bot.release()
    if failed:
        part = outpath + '.partial'
        Path(part).write_bytes(out)
        sys.exit(f'DUMP INCOMPLETE: only {len(out)}/{total} bytes read; saved partial to {part} (NOT {outpath}). '
                 f'Re-run; do not use this as a backup.')
    Path(outpath).write_bytes(out)
    print(f'saved {len(out)} bytes of flash 0x{start:x}.. to {outpath}')


MAGIC = 0x52585041
SECTOR = 0x9000          # the 4KB sector holding the shutter-lag constants
PATCH_OFF = 0x9f48       # `l.addi r18,r24,1000` (0x9e5803e8) — the deadline (main settle)
PATCH_OFF2 = 0x9f64      # `l.addi r3,r0,1000`   (0x9c6003e8) — the fast-path delay(1000) in the same block
MANAGED = {PATCH_OFF, PATCH_OFF + 1, PATCH_OFF2, PATCH_OFF2 + 1}   # the only bytes flashpatch ever changes
JEDEC = bytes([0x5e, 0x40, 0x16])


def _mkcmd(bot):
    REQ = 0x3400
    def cmd(command, addr, n, payload=b''):
        body = struct.pack('<7I', MAGIC, 1, command, addr, n, 1, 0) + payload
        if command != 2:                       # non-program requests carry no page; pad read/erase buffer
            body += b'\0' * n
        sram_write(bot, REQ, body)
        launch(bot, ENTRY, REQ)
        status = struct.unpack('<I', bot.rom_read(REQ + 20, 4))[0]
        data = bot.rom_read(REQ + 28, n) if (n and command in (0, 1)) else b''   # cmd0 JEDEC + cmd1 read
        return status, data
    return cmd


def _read_range(cmd, addr, n):
    out = bytearray(); off = 0
    while off < n:
        k = min(256, n - off)
        st, d = cmd(1, addr + off, k)
        if st != 0 or len(d) != k:
            sys.exit(f'read @0x{addr + off:x} failed (status 0x{st:x})')
        out += d; off += k
    return bytes(out)


def _write_sector(cmd, addr, data4k):
    # erase one 4KB sector, program its 16 pages from data4k, verify readback. Exits on any failure.
    assert addr & 0xfff == 0 and len(data4k) == 0x1000
    RECOVER = ' (sector may be partial; your backup has the original — re-run with WRITE to retry, or run flashrestore)'
    st, _ = cmd(3, addr, 0)
    if st != 0: sys.exit(f'erase 0x{addr:x} failed (status 0x{st:x}){RECOVER}')
    if _read_range(cmd, addr, 0x1000) != bytes([0xff]) * 0x1000:
        sys.exit(f'0x{addr:x} not all-0xFF after erase (block-protect / bad erase); aborting before program{RECOVER}')
    for p in range(16):
        st, _ = cmd(2, addr + p * 256, 256, bytes(data4k[p * 256:(p + 1) * 256]))
        if st != 0: sys.exit(f'program 0x{addr + p*256:x} failed (status 0x{st:x}){RECOVER}')
    back = _read_range(cmd, addr, 0x1000)
    if back != bytes(data4k):
        bad = [addr + i for i in range(0x1000) if back[i] != data4k[i]]
        sys.exit(f'verify 0x{addr:x} mismatch at {len(bad)} bytes {bad[:4]}{RECOVER}')


def _upload_payload(bot, window):
    # Build+upload a payload whose erase/program is HARD-restricted to [window) (compile-time guard).
    blob = flstub.build(window=window)
    sram_write(bot, ENTRY, blob)
    if bot.rom_read(ENTRY, len(blob)) != blob:
        sys.exit('flasher upload readback mismatch; stop')
    print(f'  payload uploaded ({len(blob)} B, write-window 0x{window[0]:x}-0x{window[1]:x})')
    return _mkcmd(bot)


def _granule_preflight(bot):
    # Prove opcode 0x20 erases EXACTLY 4KB (device-wide NOR property), using a BLANK scratch 64K block.
    # If 0x20 erased a 64K block instead, erasing 0x9000 (same 64K block as the boot sector) would wipe
    # sector 0 -> unbootable. The test is done in blank flash: even a 64K erase there destroys nothing.
    # Writes are destructive, so this must run ONLY on a committed WRITE (not a dry run).
    SCR, BLOCK = 0x200000, 0x10000                   # 64K-aligned scratch; whole block a 64K erase could hit
    A, B = SCR, SCR + 0x1000                          # two adjacent 4KB sectors inside that block
    cmd = _upload_payload(bot, (SCR, SCR + BLOCK))    # window spans the whole block (for the reset erases)
    st, jd = cmd(0, 0, 3)
    if st != 0 or jd != JEDEC: sys.exit('granule preflight: JEDEC probe failed')
    # the WHOLE 64K block must be blank apart from leftover markers in A/B (so we never clobber live data,
    # and an interrupted previous run self-heals rather than blocking)
    block = _read_range(cmd, SCR, BLOCK)
    # Every byte must be 0xFF, except the first 256 bytes of A and of B, where a leftover 0xA5 marker
    # from an interrupted prior run is allowed. Anything else means real data we must NOT erase.
    MARKER_B = 0xA5
    stray = []
    for off in range(BLOCK):
        b = block[off]
        if b == 0xff:
            continue
        in_marker = (0 <= off < 256) or (0x1000 <= off < 0x1100)   # A[0:256] or B[0:256]
        if in_marker and b == MARKER_B:
            continue
        stray.append(SCR + off)
    if stray:
        sys.exit(f'granule preflight: scratch block 0x{SCR:x} has unexpected data at {[hex(x) for x in stray[:4]]} '
                 f'({len(stray)} bytes); refusing to erase it')
    for s in (A, B):                                  # reset A,B to blank (clears any interrupted prior run)
        st, _ = cmd(3, s, 0)
        if st != 0: sys.exit(f'granule preflight: scratch reset erase @0x{s:x} failed (status 0x{st:x})')
    if _read_range(cmd, A, 0x2000) != bytes([0xff]) * 0x2000:
        sys.exit('granule preflight: scratch did not go blank after reset erase')
    marker = bytes([0xA5]) * 256
    for s in (A, B):
        st, _ = cmd(2, s, 256, marker)
        if st != 0: sys.exit(f'granule preflight: marker program @0x{s:x} failed (status 0x{st:x})')
    st, _ = cmd(3, A, 0)                              # erase ONLY sector A
    if st != 0: sys.exit(f'granule preflight: test erase @0x{A:x} failed (status 0x{st:x})')
    a_blank = _read_range(cmd, A, 256) == bytes([0xff]) * 256
    b_intact = _read_range(cmd, B, 256) == marker
    print(f'  granule test: 0x{A:x} erased->blank={a_blank}, neighbour 0x{B:x} intact={b_intact}')
    if not (a_blank and b_intact):
        sys.exit('ERASE GRANULE IS NOT 4KB — 0x20 wiped the neighbouring sector! '
                 'Refusing to touch 0x9000 (it shares a 64K block with the boot sector).')
    st, _ = cmd(3, B, 0)                              # cleanup: leave scratch blank, verified, for next run
    if st != 0 or _read_range(cmd, A, 0x2000) != bytes([0xff]) * 0x2000:
        sys.exit(f'granule preflight: scratch cleanup incomplete (status 0x{st:x}); manually erase 0x{A:x}/0x{B:x} before retrying')
    print('  CONFIRMED: 0x20 = 4KB sector erase on this chip; erasing 0x9000 stays within its own 4KB sector.')


def _build_patched(backup, ticks):
    # Patch BOTH delay immediates in the capture block to `ticks`:
    #   0x9f48 `l.addi r18,r24,1000` (0x9e58....) — the main deadline/settle
    #   0x9f64 `l.addi r3,r0,1000`   (0x9c60....) — the fast-path delay() arg (only hit if setup <5 ticks)
    assert 1 <= ticks <= 32767, 'ticks must be 1..32767 (a positive l.addi immediate; >32767 would be negative)'
    cur = struct.unpack_from('<I', backup, PATCH_OFF)[0]
    cur2 = struct.unpack_from('<I', backup, PATCH_OFF2)[0]
    if (cur >> 16) != 0x9e58 or (cur & 0xffff) != 1000:
        sys.exit(f'backup word @0x{PATCH_OFF:x} is 0x{cur:08x}, not `l.addi r18,r24,1000`; refusing')
    if (cur2 >> 16) != 0x9c60 or (cur2 & 0xffff) != 1000:
        sys.exit(f'backup word @0x{PATCH_OFF2:x} is 0x{cur2:08x}, not `l.addi r3,r0,1000`; refusing')
    patched = bytearray(backup[SECTOR:SECTOR + 0x1000])
    struct.pack_into('<I', patched, PATCH_OFF - SECTOR, (cur & 0xffff0000) | ticks)
    struct.pack_into('<I', patched, PATCH_OFF2 - SECTOR, (cur2 & 0xffff0000) | ticks)
    diff = {SECTOR + i for i in range(0x1000) if patched[i] != backup[SECTOR + i]}
    assert diff <= MANAGED, f'unexpected diff {sorted(hex(x) for x in diff)}; refusing'
    return bytes(patched), cur


def flashpatch(backup_path, ticks, confirm):
    # Lower the shutter-lag constant and reflash ONLY sector 0x9000. Dry-run unless confirm=='WRITE'.
    backup = open(backup_path, 'rb').read()
    assert len(backup) >= 0x10000, 'backup too short (need >=64KB)'
    orig_sector = backup[SECTOR:SECTOR + 0x1000]
    patched, cur = _build_patched(backup, ticks)
    print(f'patch: delay immediates 0x{PATCH_OFF:x} and 0x{PATCH_OFF2:x}: 1000 -> {ticks} ticks (<=4 bytes in sector 0x{SECTOR:x})')

    bot = enter_loader()
    # READ-ONLY phase (also the whole of a dry run): JEDEC + live-sector gate, no flash writes.
    cmd = _upload_payload(bot, (SECTOR, SECTOR + 0x1000))   # payload can ONLY erase/program sector 0x9000
    st, jd = cmd(0, 0, 3)
    print(f'  cmd0 init+JEDEC: status 0x{st:x} JEDEC {jd.hex()} (expect 5e4016)')
    if st != 0 or jd != JEDEC:
        bot.release(); sys.exit('JEDEC probe failed; refusing to write')
    live = _read_range(cmd, SECTOR, 0x1000)
    if live == patched:
        print('  live sector ALREADY matches the patched image — nothing to do.'); bot.release(); return
    # the live sector may already carry a PRIOR delay patch; tolerate differences only in the managed
    # delay bytes, and require everything else to still match the backup.
    managed_rel = {o - SECTOR for o in MANAGED}
    unexpected = [SECTOR + i for i in range(0x1000) if i not in managed_rel and live[i] != orig_sector[i]]
    if unexpected:
        bot.release(); sys.exit(f'LIVE sector 0x{SECTOR:x} differs from backup OUTSIDE the delay bytes at '
                                f'{len(unexpected)} offsets (first {unexpected[:4]}); run flashrestore {backup_path} WRITE first. Refusing.')
    print(f'  live sector 0x{SECTOR:x} matches backup outside the delay immediates — safe to proceed')

    if confirm != 'WRITE':
        print('\nDRY RUN (nothing written — only JEDEC + sector read). Re-run with final arg WRITE to erase+program.')
        bot.release(); return

    # COMMITTED WRITE path only: prove the erase granule (writes blank scratch), then patch 0x9000.
    print('\nerase-granule preflight (writes a blank scratch block, proves 0x20 = 4KB)...')
    _granule_preflight(bot)
    cmd = _upload_payload(bot, (SECTOR, SECTOR + 0x1000))   # re-upload the 0x9000-windowed payload
    print('\n*** WRITING sector 0x9000 (erase + 16 pages + verify) ***')
    _write_sector(cmd, SECTOR, patched)        # guarded to 0x9000; exits with a recovery hint on any failure
    print('  VERIFY OK: sector matches patched image; only the delay immediates (0x9f48/0x9f64) changed.')
    print('  DONE. Power-cycle the camera and test the shutter.')
    bot.release()


def _find_wavs(data):
    # locate every RIFF/WAVE and its data-chunk size field: returns [(start, data_size_field_off, data_size)]
    out = []; i = 0
    while True:
        s = data.find(b'RIFF', i)
        if s < 0: break
        i = s + 1
        if data[s + 8:s + 12] != b'WAVE': continue
        riffsz = struct.unpack_from('<I', data, s + 4)[0]; p = s + 12
        while p + 8 <= min(len(data), s + 12 + riffsz):
            cid = data[p:p + 4]; csz = struct.unpack_from('<I', data, p + 4)[0]
            if cid == b'data':
                out.append((s, p + 4, csz)); break
            p += 8 + csz + (csz & 1)
    return out


def flash_edits(backup, edits, confirm, strict=True):
    # edits: {flash_offset: new_byte}. Rewrite each affected 4KB sector to backup+edits.
    # strict: require the live sector to still match the backup (or already be the target) before writing.
    # Non-strict (e.g. re-patching over a prior change) trusts backup+edits as authoritative and relies on
    # the granule preflight + readback verify for safety.
    assert edits
    sectors = sorted(set(off & ~0xfff for off in edits))
    targets = {}
    for sec in sectors:
        patched = bytearray(backup[sec:sec + 0x1000])
        for o, v in edits.items():
            if sec <= o < sec + 0x1000: patched[o - sec] = v
        targets[sec] = bytes(patched)
    bot = enter_loader()
    cmd = _upload_payload(bot, (sectors[0], sectors[0] + 0x1000))
    st, jd = cmd(0, 0, 3)
    print(f'  cmd0 init+JEDEC: status 0x{st:x} JEDEC {jd.hex()} (expect 5e4016)')
    if st != 0 or jd != JEDEC: bot.release(); sys.exit('JEDEC probe failed; refusing')
    todo = []
    for sec in sectors:
        cmd = _upload_payload(bot, (sec, sec + 0x1000))
        live = _read_range(cmd, sec, 0x1000)
        if live == targets[sec]:
            print(f'  sector 0x{sec:x}: already matches target — will skip'); continue
        if strict and live != backup[sec:sec + 0x1000]:
            bad = [sec + k for k in range(0x1000) if live[k] != backup[sec + k]]
            bot.release(); sys.exit(f'live sector 0x{sec:x} differs from backup at {len(bad)} bytes {bad[:4]} and is not the target; refusing')
        nchg = sum(1 for k in range(0x1000) if live[k] != targets[sec][k])
        todo.append(sec)
        tag = 'matches backup' if live == backup[sec:sec + 0x1000] else 'differs from backup (non-strict)'
        print(f'  sector 0x{sec:x}: {tag}; {nchg} byte(s) will change')
    print(f'  total sectors to write: {len(todo)}')
    if confirm != 'WRITE':
        print('\nDRY RUN (nothing written — JEDEC + sector reads only). Re-run with final arg WRITE.'); bot.release(); return
    print('\nerase-granule preflight (writes a blank scratch block, proves 0x20 = 4KB)...')
    _granule_preflight(bot)
    for sec in todo:
        cmd = _upload_payload(bot, (sec, sec + 0x1000))
        if _read_range(cmd, sec, 0x1000) == targets[sec]:
            print(f'  sector 0x{sec:x} already patched — skip'); continue
        print(f'  *** WRITING sector 0x{sec:x} ***')
        _write_sector(cmd, sec, targets[sec])
    print('  DONE. Power-cycle the camera and test.')
    bot.release()


def silence_sounds(backup_path, confirm):
    # Silence the power-on/off jingles (the two longest WAVs) by zeroing their PCM SAMPLES to 0x00
    # (= silence). The player ignores the WAV header length, so zeroing the samples themselves is the
    # reliable kill; the format header (sample rate etc.) is preserved. ~110KB over ~28 sectors.
    backup = open(backup_path, 'rb').read()
    wavs = sorted(_find_wavs(backup), key=lambda w: -w[2])
    if len(wavs) < 2: sys.exit('could not find two WAVs in the backup')
    power = wavs[:2]
    edits = {}
    print('silencing the 2 longest WAVs (power on/off jingles) by zeroing their samples:')
    for s, dszoff, dsz in power:
        payload = dszoff + 4                          # samples start right after the data-size field
        print(f'  WAV @0x{s:x}  samples 0x{payload:x}..0x{payload+dsz:x} ({dsz}B, ~{dsz/(16000*2):.2f}s) -> silence')
        for o in range(payload, payload + dsz):
            edits[o] = 0
    secs = sorted(set(o & ~0xfff for o in edits))
    print(f'  {len(edits)} sample bytes across {len(secs)} sectors (0x{secs[0]:x}..0x{secs[-1]:x}); headers kept')
    flash_edits(backup, edits, confirm, strict=False)  # non-strict: re-patches over the earlier header change


def flashrestore(backup_path, confirm):
    # Recovery: rewrite sector 0x9000 from the backup (no equality gate). Dry-run unless confirm=='WRITE'.
    backup = open(backup_path, 'rb').read()
    assert len(backup) >= 0x10000, 'backup too short'
    orig_sector = backup[SECTOR:SECTOR + 0x1000]
    bot = enter_loader()
    cmd = _upload_payload(bot, (SECTOR, SECTOR + 0x1000))
    st, jd = cmd(0, 0, 3)
    print(f'  cmd0 init+JEDEC: status 0x{st:x} JEDEC {jd.hex()}')
    if st != 0 or jd != JEDEC: bot.release(); sys.exit('JEDEC probe failed; refusing')
    live = _read_range(cmd, SECTOR, 0x1000)
    print(f'  live sector 0x{SECTOR:x} == backup? {live == orig_sector}')
    if confirm != 'WRITE':
        print('\nDRY RUN (nothing written — only JEDEC + sector read). Re-run with final arg WRITE to erase+reprogram from backup.')
        bot.release(); return
    print('\nerase-granule preflight (writes a blank scratch block, proves 0x20 = 4KB)...')
    _granule_preflight(bot)
    cmd = _upload_payload(bot, (SECTOR, SECTOR + 0x1000))   # re-upload the 0x9000-windowed payload
    print('\n*** RESTORING sector 0x9000 from backup ***')
    _write_sector(cmd, SECTOR, orig_sector)
    print('  RESTORE OK: sector 0x9000 matches backup. Power-cycle to return to stock.')
    bot.release()


def diag(which):
    # Diagnostic ladder: does reading an SPR in the loader fault? Upload a minimal stub that
    # reads one SPR and returns; retrieve stage+value via SEPARATE reads (marker-proven path).
    stub = (HERE.parent / 'tools' / f'stub_diag_{which}.bin').read_bytes()
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
    elif len(sys.argv) >= 2 and sys.argv[1] == 'flashpatch':
        # flashpatch BACKUP [TICKS=200] [WRITE]   (dry run unless last arg is exactly WRITE)
        bpath = sys.argv[2] if len(sys.argv) > 2 else str(HERE / 'full_backup_4mb.bin')
        tk = int(sys.argv[3], 0) if len(sys.argv) > 3 else 200
        cf = sys.argv[4] if len(sys.argv) > 4 else 'DRY'
        flashpatch(bpath, tk, cf)
    elif len(sys.argv) >= 2 and sys.argv[1] == 'flashrestore':
        # flashrestore BACKUP [WRITE]   (restores sector 0x9000 from backup; dry run unless WRITE)
        bpath = sys.argv[2] if len(sys.argv) > 2 else str(HERE / 'full_backup_4mb.bin')
        cf = sys.argv[3] if len(sys.argv) > 3 else 'DRY'
        flashrestore(bpath, cf)
    elif len(sys.argv) >= 2 and sys.argv[1] == 'silence-sounds':
        # silence-sounds BACKUP [WRITE]   (zero the 2 longest WAVs' size fields; dry run unless WRITE)
        bpath = sys.argv[2] if len(sys.argv) > 2 else str(HERE / 'full_backup_4mb.bin')
        cf = sys.argv[3] if len(sys.argv) > 3 else 'DRY'
        silence_sounds(bpath, cf)
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
