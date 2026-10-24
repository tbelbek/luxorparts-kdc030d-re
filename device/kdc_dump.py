#!/usr/bin/env python3
"""Read-only dump for the Luxorparts KDC-030D (Buildwin/AppoTech, USB 0219:3280).

  probe  standard SCSI INQUIRY only (nothing vendor-specific is sent)
  dump   0xDA -> BootROM loader (1908:3319), then the ROM's own 0xCB memory read:
         16 bytes of BootROM vs the JT5295/AX3295B references, the 0xCB read-path code,
         then RAM 2 MiB twice.

Sends no RAM/flash write command and uploads no code. Before 0xDA the stock USB
descriptors must match the SDK build of the reference firmwares; before the first 0xCB
the loader's INQUIRY/descriptors/strings must match both reference BootROMs. Residual
risk: the first 0xCB jumps to 0x00102620 before its code can be read back.
The loader resets itself ~2-3 s after the last USB traffic (back to stock firmware).
Protocol: github.com/matu6968/Action_Digital_Kids_Camera_DOOM (tools/python/apxtool).
macOS: run with sudo (libusb needs root to detach the mass-storage driver).
"""
import hashlib, os, struct, sys, time
from pathlib import Path
import usb.core, usb.util, usb.backend.libusb1

BACKEND = usb.backend.libusb1.get_backend(find_library=lambda _: os.environ.get('KDC_LIBUSB','/opt/homebrew/lib/libusb-1.0.dylib'))
STOCK_IDS = ((0x0219, 0x3280), (0x1908, 0x3283))
LOADER_ID = (0x1908, 0x3319)
ROM_FN = 0x00102620                      # BootROM 0xCB memory helper, same address in both known ROMs
ROM_BASE = 0x00100000
# Reference BootROMs (NOT redistributed here — fetch from upstream, see README):
#   soc_boot_rom.bin      from github.com/hidoba/penguin-camera (assets/rom/)
#   bootrom_matu_64k.bin  from github.com/matu6968/Action_Digital_Kids_Camera_DOOM (dumps/)
ROMDIR = Path(os.environ.get('KDC_ROM_DIR', Path(__file__).resolve().parent.parent / 'refs'))
ROM_REFS = {
    ROMDIR / 'bootrom_matu_64k.bin': 'f5b68ff45bff5f4f21c49008ed0a4bcbab01f2a2cb5aeff8f832ee41be460e39',  # JT5295 64K
    ROMDIR / 'soc_boot_rom.bin': '62d4dabeeca22efddc11ea0fa95a5a0823e1897e35659c9b5ae0bc47f4205eef',      # AX3295B 32K
}
HELPER = ((0x2530, 0x2584), (0x2620, 0x265C))   # the read path 0xCB executes; identical in both ROMs
# Fingerprints checked before any vendor command. STOCK_*: this camera's descriptors, found
# byte-identical (with USB-SDK stamp 20160418V1.000) in both reference stock firmwares,
# whose shared SCSI dispatcher handles 0xDA. LOADER_*: identical in both reference BootROMs.
STOCK_DEV = bytes.fromhex('120110010000004019028032000101020001')
STOCK_CFG = bytes.fromhex('0902200001010080320904040002080650000705810200020107050102000201')
STOCK_SDK = '20160418V1.000'
LOADER_INQUIRY = bytes.fromhex('008002022c0000004275696c6457696e566964656f3035304c6f616465722020312e3030')
LOADER_DEV = {bytes.fromhex('120100020000004008191933000101020001'), bytes.fromhex('120110010000004008191933000101020001')}
LOADER_CFG = {bytes.fromhex('0902200001010080300904000002080650000705810240000007050102400000'),
              bytes.fromhex('0902200001010080300904000002080650000705810200020007050102000200')}
RAM_BASE, RAM_SIZE, CHUNK = 0x02000000, 0x200000, 0x1000   # 2 MiB: smallest RAM seen in this family


class Bot:
    """USB mass-storage Bulk-Only Transport on the device's mass-storage interface."""
    def __init__(self, dev):
        self.dev, self.tag = dev, 0x4B440000
        intf = usb.util.find_descriptor(dev.get_active_configuration(), bInterfaceClass=8)
        if intf is None or (intf.bInterfaceSubClass, intf.bInterfaceProtocol) != (6, 0x50):
            raise RuntimeError('no SCSI/BOT mass-storage interface')
        self.iface = intf.bInterfaceNumber
        bulk = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
        self.ep_in = next(e.bEndpointAddress for e in bulk if e.bEndpointAddress & 0x80)
        self.ep_out = next(e.bEndpointAddress for e in bulk if not e.bEndpointAddress & 0x80)
        if dev.is_kernel_driver_active(self.iface):
            dev.detach_kernel_driver(self.iface)
        usb.util.claim_interface(dev, self.iface)

    def cmd(self, cdb, length=0, timeout=1500):
        self.tag += 1
        cbw = struct.pack('<4sIIBBB16s', b'USBC', self.tag, length, 0x80 if length else 0, 0, len(cdb), cdb)
        if self.dev.write(self.ep_out, cbw, timeout=timeout) != 31:
            raise RuntimeError('short CBW')
        data = bytes(self.dev.read(self.ep_in, length, timeout=timeout)) if length else b''
        csw = bytes(self.dev.read(self.ep_in, 13, timeout=timeout))
        if len(csw) != 13 or struct.unpack('<4sIIB', csw) != (b'USBS', self.tag, 0, 0) or len(data) != length:
            raise RuntimeError(f'bad reply: csw={csw.hex()} data={len(data)}/{length}')
        return data

    def inquiry(self):
        d = self.cmd(b'\x12\x00\x00\x00\x24\x00', 36)
        return norm(d[8:16].decode(errors='replace')), norm(d[16:32].decode(errors='replace'))

    def rom_read(self, address, length):
        return self.cmd(bytes([0xCB]) + struct.pack('<III', ROM_FN, address, 0xFFFFFFFF) + b'\xff' * 3, length)

    def release(self):
        try:
            usb.util.release_interface(self.dev, self.iface)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self.dev)


def norm(s): return ''.join(c for c in s.casefold() if c.isalnum())


def descriptors(dev):
    """Standard GET_DESCRIPTOR reads: device (18 bytes) and full configuration."""
    device = bytes(dev.ctrl_transfer(0x80, 6, 0x0100, 0, 18))
    total = int.from_bytes(bytes(dev.ctrl_transfer(0x80, 6, 0x0200, 0, 9))[2:4], 'little')
    return device, bytes(dev.ctrl_transfer(0x80, 6, 0x0200, 0, total))


def find(ids):
    found = [d for vid, pid in ids for d in usb.core.find(find_all=True, idVendor=vid, idProduct=pid, backend=BACKEND)]
    if len(found) > 1:
        sys.exit('more than one camera connected; stop')
    return found[0] if found else None


def stock_session():
    dev = find(STOCK_IDS)
    if not dev:
        sys.exit('camera not found in stock USB mode (0219:3280 / 1908:3283)')
    print(f'stock device {dev.idVendor:04x}:{dev.idProduct:04x}')
    if descriptors(dev) != (STOCK_DEV, STOCK_CFG) or usb.util.get_string(dev, 3) != STOCK_SDK:
        sys.exit('stock USB descriptors differ from the reference SDK build; stop')
    bot = Bot(dev)
    ident = bot.inquiry()
    print(f'INQUIRY: {ident}')
    if ident != ('buildwin', 'mediaplayer'):
        sys.exit('not Buildwin Media-Player; stop')
    return bot


def open_loader(deadline):
    """Wait for 1908:3319 and claim it; darwin detach re-enumerates, so retry until deadline."""
    seen = None
    while time.monotonic() < deadline:
        dev = find([LOADER_ID])
        if dev:
            seen = seen or time.monotonic()
            try:
                bot = Bot(dev)
                ident = bot.inquiry()
                print(f'loader INQUIRY {ident} after {time.monotonic() - seen:.2f}s')
                return bot, dev, ident
            except (usb.core.USBError, RuntimeError):
                usb.util.dispose_resources(dev)
        time.sleep(0.02)
    sys.exit('loader did not answer (camera returns to stock by itself; power-cycle if not)')


def read_all(bot, base, size):
    out = bytearray()
    for off in range(0, size, CHUNK):
        out += bot.rom_read(base + off, min(CHUNK, size - off))
    return bytes(out)


def dump(outdir):
    refs = [p.read_bytes() for p in ROM_REFS]
    if [hashlib.sha256(r).hexdigest() for r in refs] != list(ROM_REFS.values()):
        sys.exit('reference BootROM files missing or wrong')
    outdir.mkdir(parents=True)          # fails if it exists: never overwrite an old run
    bot = stock_session()
    print('sending 0xDA (enter BootROM loader)')
    try:
        bot.cmd(bytes([0xDA]) + b'\xff' * 15)
    except usb.core.USBError as exc:    # device may drop off the bus before its CSW
        print(f'0xDA: no CSW ({exc}); waiting for the loader anyway')
    bot.release()
    bot, dev, ident = open_loader(time.monotonic() + 15)
    device, config = descriptors(dev)
    product, maker = usb.util.get_string(dev, 2), usb.util.get_string(dev, 1)
    raw = bot.cmd(b'\x12\x00\x00\x00\x24\x00', 36)
    print(f'loader USB {maker!r} {product!r}')
    if (ident != ('buildwin', 'video050loader') or raw != LOADER_INQUIRY or device not in LOADER_DEV
            or config not in LOADER_CFG or {maker, product} != {'BUILDWIN', 'BLDR v1.00'}):
        sys.exit('loader fingerprint differs from both reference BootROMs; stop before any 0xCB')
    head = bot.rom_read(ROM_BASE, 16)
    if head not in [r[:16] for r in refs]:
        sys.exit(f'first 16 BootROM bytes {head.hex()} match no reference; stop')
    for lo, hi in HELPER:                          # small reads of the code 0xCB runs, before any bulk read
        if bot.rom_read(ROM_BASE + lo, hi - lo) != refs[0][lo:hi]:
            sys.exit(f'0xCB read-path code at 0x{ROM_BASE + lo:x} differs from the references; stop')
    rom = read_all(bot, ROM_BASE, 0x8000)          # 32 KiB exists on every known ROM
    if rom == refs[0][:0x8000]:
        rom += read_all(bot, ROM_BASE + 0x8000, 0x8000)
    known = {hashlib.sha256(r).hexdigest() for r in refs}
    print(f'BootROM {len(rom) // 1024} KiB: {"known" if hashlib.sha256(rom).hexdigest() in known else "NEW (helper code identical)"}')
    passes = [read_all(bot, RAM_BASE, RAM_SIZE) for _ in (1, 2)]
    bot.release()
    (outdir / 'bootrom.bin').write_bytes(rom)
    for n, data in enumerate(passes, 1):
        (outdir / f'ram-{n}.bin').write_bytes(data)
        print(f'RAM pass {n}: sha256 {hashlib.sha256(data).hexdigest()}')
    print('RAM passes identical' if passes[0] == passes[1] else 'RAM passes differ (live RAM; compare the code region)')
    print(f'saved to {outdir}; power-cycle the camera to return to stock')


if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in ('probe', 'dump') or (sys.argv[1] == 'dump' and len(sys.argv) != 3):
        sys.exit('usage: kdc_dump.py probe | dump OUTDIR')
    if sys.argv[1] == 'probe':
        stock_session().release()
    else:
        dump(Path(sys.argv[2]))
