/* KDC-030D firmware — hand-decompiled key functions (readable C)
 *
 * Reconstructed from our own little-endian OpenRISC disassembly (tools/or1k_dis.py), by hand, because
 * stock decompilers mis-model this SoC's call/branch slots (no delay slot; l.jal links r9=PC+4; a taken
 * branch skips the next instruction). RAM<->flash for the app: flash = RAM - 0x01ffdc00.
 * Addresses in comments are the RAM address of each instruction. These are faithful to the control flow
 * and the observable effects; incidental register scheduling is omitted for readability.
 */
#include <stdint.h>

/* ---- timing primitives (RAM 0x0201f10c / 0x0201f12c) ----
 * A free-running ~1 kHz millisecond counter lives at 0x02081284. */
#define TICKS (*(volatile uint32_t *)0x02081284)

uint32_t uptime(void)            /* 0x0201f10c */
{
    return TICKS;                /* movhi/ori 0x02081284 ; lwz r11,0(r3) ; jr r9 */
}

void delay(uint32_t ms)          /* 0x0201f12c */
{
    uint32_t start = TICKS;      /* 0x0201f13c */
    if (TICKS == 0) return;      /* 0x0201f144  guard: counter not running yet */
    uint32_t target = start + ms;/* 0x0201f14c */
    while (TICKS < target)       /* 0x0201f150..58  busy-wait (unsigned; wraps at ~49.7 days) */
        ;
}

/* ---- the shutter-lag gate (inside the capture routine, func @ 0x020078cc) ----
 * `t0` = uptime() sampled earlier (0x020079fc), after the sensor/subsystem readiness waits.
 * This enforces a MINIMUM elapsed time (an AE/AWB settle margin) before the frame grab.
 * BOTH 1000 immediates were patched 1000 -> 250 by `flashpatch` (flash 0x9f48 and 0x9f64). */
void capture_settle(uint32_t t0)
{
    uint32_t deadline = t0 + 1000;          /* 0x02007b48  l.addi r18,r24,1000   (PATCH @0x9f48) */
    if (uptime() <= deadline) {             /* 0x02007b4c..54 */
        if (uptime() <= t0 + 5)             /* 0x02007b58..68  "setup finished in <5 ticks" fast path */
            delay(1000);                    /* 0x02007b64  l.addi r3,r0,1000     (PATCH @0x9f64) */
        else
            delay(deadline - uptime());     /* 0x02007b6c..74  wait the remaining time */
    }
    frame_grab();                           /* 0x02007b78  l.jal 0x02007164 */
}

/* ---- sensor register write over I2C/SCCB (RAM 0x02047a34) ----
 * `buf` points at {reg, val} (8-bit regs) or {reg_hi, reg_lo, val} (16-bit regs). The addressing width
 * is chosen by a global mode byte; the sensor's current I2C address is held in two globals. For the
 * SP0A39 (8-bit registers) this takes the 8-bit path. This is the function every sensor init write and
 * the AWB/quality tooling targets. */
void sccb_write(const uint8_t *buf)         /* 0x02047a34 (verified w/ Codex) */
{
    uint8_t  addr       = *(uint8_t *)0x02082f6f;       /* sensor I2C address (global) */
    uint8_t  count      = *(uint8_t *)0x02082f6c;       /* payload byte count (global) */
    uint8_t  width_mode = *(uint8_t *)0x02082f6d;       /* 0x02047a4c..50 */
    if (width_mode == 2) {                              /* 16-bit register sensors */
        uint16_t reg = (buf[0] << 8) | buf[1];          /* 0x02047a68..7c */
        i2c_write_16bit(addr, reg, buf + 2, count);     /* 0x02047a84 -> 0x02033800 */
    } else {                                            /* 8-bit register sensors (SP0A39) */
        i2c_write_8bit(addr, buf[0], buf + 1, count);   /* 0x02047a9c -> 0x02033500 */
    }
}

/* ---- JPEG quantization/Huffman table loader (RAM 0x0202c788) ----
 * Copies the chosen quant set into the codec's table SRAM, reordering natural-order quant bytes into
 * zigzag order via an index array, then loads the Huffman tables. Two quant sets exist in flash:
 *   set A @ RAM 0x0206a564 (flash 0x6c964)  ~Q45  (selector != 0)
 *   set B @ RAM 0x0206ada4 (flash 0x6d1a4)  ~Q45, THIS is the still-photo table (selector 0)
 * `jpeg-quality` rewrites set B's 128 bytes (luma+chroma) to a higher-quality Annex-K-scaled table.
 * ZZ[] is the JPEG zigzag index table the copy loop walks. */
/* ZZ[] = 64 32-bit zigzag indices at RAM 0x0206ae24. */
void jpeg_load_tables(int selector)         /* 0x0202c788 / body 0x0202c7a0 (verified w/ Codex) */
{
    /* only the low byte of `selector` is tested (so e.g. 256 selects set B) */
    const uint8_t *quant = (selector & 0xff) ? (const uint8_t *)0x0206a564    /* set A */
                                             : (const uint8_t *)0x0206ada4;   /* set B (still photo) */
    /* quant: luma 64 -> SRAM 0x064000, chroma 64 -> 0x064040, natural->zigzag via ZZ[] */
    for (int i = 0; i < 64; i++)                         /* 0x0202c7b4..e0 / 0x0202c820..4c */
        *(volatile uint8_t *)(0x064000 + i) = quant[ ZZ[i] ];
    for (int i = 0; i < 64; i++)                         /* 0x0202c7e4..810 / 0x0202c850..7c */
        *(volatile uint8_t *)(0x064040 + i) = quant[ 64 + ZZ[i] ];
    /* then two SELECTOR-INDEPENDENT blocks (Huffman / JFIF tables) are copied verbatim: */
    memcpy((void *)0x064080, (void *)0x0206a5e4, 0x380); /* 0x0202c884..b4 */
    memcpy((void *)0x064800, (void *)0x0206a964, 0x440); /* 0x0202c8c0..f0 */
}
