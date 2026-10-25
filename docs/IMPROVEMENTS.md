# Additional firmware-improvement opportunities (scanned with Codex + sourced sibling-driver decode)

Beyond what's already shipped (shutter-lag+GUARD, silenced jingles, photo JPEG Q90, AWB override,
print-tone, debug/dead-code). Found by a delay-site + sensor-register scan, cross-checked with Codex and
a sourced decode of the **exact** sensor's register map (Espressif `esp-video-components` SP0A39 header,
which carries inline labels and verbatim confirms our AWB map; siblings SP0A20/SP0A19 fill gaps).

**Two mechanisms, from the project's hard constraint (packed image, no safe home for added code):**
- **in-place operand patch** — change the immediate(s) of an instruction the firmware already runs. Safe,
  reversible, same tier as the shutter/AWB patches. Only possible for a register the firmware ALREADY writes.
- **trailing-write repurpose** — the exact method the AWB fix used: convert a run of existing sensor-init
  writes (operand-only) into the writes we want. Safe/reversible too, but it SACRIFICES those original
  writes and needs a "write budget" on the right sensor page. This is how a register the firmware does NOT
  currently write can still be added WITHOUT code injection.
- (Genuine code injection has no safe home here — see FINDINGS.md — so nothing below needs it.)

## Ranked opportunities

### 1. Indoor brightness — AE target  ★ highest image payoff  — ✅ SHIPPED (optional `awb … AE`)
The cleanest brightness knob on this sensor is the **auto-exposure target**: page-1 `0xcb` (indoor target,
stock ~0x38) and `0xcc` (outdoor). Raising it brightens dim indoor shots (a classic cheap-kids-camera
complaint). The firmware does NOT write 0xcb/0xcc today, so deliver it the AWB way: repurpose a short run of
page-1 trailing init writes into `0xfd=1; 0xcb=<higher>` (optionally `0xcc`). **Mechanism: trailing-write
repurpose (like AWB). Value: high. Confidence: register CONFIRMED-labeled in the esp SP0A39 header; exact
target value is tune-on-device. Risk: medium (needs a page-1 write budget + verify nothing later resets AE).**
Source: esp sp0a39 header AE block (`rpc_target` 0xcb/0xcc).

### 2. Low-light brightness vs noise — AE max-gain ceiling
Page-1 `0x04` (indoor max gain, ~0xff) / `0x0a` (outdoor, ~0x30) cap sensor gain — the classic brightness/
noise trade. Lowering them cuts low-light grain (at the cost of darker/longer exposure); raising brightens
noisier. Pair with `0x06` (max exposure) for the brightness-vs-motion-blur trade. Not written today →
trailing-write repurpose. **Value: medium-high. Confidence: labeled. Risk: medium (interacts with #1).**

### 3. Snappier mode-switch / record start-stop — status delays
Seven `delay(2000)` and several `delay(1000)` sites sit in the capture/video region (RAM 0x02009xxx and
0x0200cxxx, around 'PHO0000.JPG'/'720P'/'1080P'), each right after a call to the 25-caller state function
0x0204bf98. **Mechanism: in-place operand patch (change the 2000/1000 immediate).** BUT — and this corrects
an earlier over-broad "all off-path delays are hardware settles" note in FINDINGS — some of these may be
SD/DMA completion waits, not just UI pauses. **Do not blanket-shorten.** Per site: trace what 0x0204bf98
sets and whether a completion/poll follows; shorten ONE, test record-stop file integrity + rapid repeats,
then proceed. **Value: medium (UX). Confidence: medium. Risk: medium/high until traced.**

### 4. Sharpness — edge enhancement
Page-1 `0xe2` (sharpen base, ~0x50) / `0xe4` (sharpen max, ~0xa0), plus the range/factor banks. Not written
today → trailing-write repurpose. A modest raise crisps the soft VGA output; too high = halos/noise.
**Value: medium. Confidence: labeled (SP0A19/SP0A39). Risk: medium.**

### 5. Native-resolution default
The sensor is VGA 640×480; every larger "photo size" is an upscale (bigger files, slower save, no real
detail). Defaulting the camera to its native/closest-to-native size saves storage + speeds saving. **Mechanism:
in-place DATA patch IF the default size index is a stored constant/table entry (needs locating); otherwise a
menu-default repurpose.** **Value: medium (storage/speed). Confidence: high rationale, must locate the size
table. Risk: medium (encoder buffers/headers must agree — prefer an EXISTING mode, don't invent dimensions).**

### 6. Denoise / saturation / gamma via already-written registers
The only quality regs the firmware ACTUALLY writes (so patchable purely in-place) are `0x56` (denoise dummy-
bank threshold), `0xb4` (saturation), and `0x70`/`0x83` (gamma points). Caveats: (a) `0x56` and `0xb4` are
among the five trailing writes the AWB patch repurposes — you can't use both on the same unit; (b) a single
gamma point (0x70/0x83) only nudges the curve; (c) page attribution for some of these is uncertain (see
below). **Value: low-medium. Confidence: low-medium. Risk: medium.** Lowest priority; marginal vs #1/#2.

### 7. Design-only (would need multi-register blocks ⇒ a large repurpose or injection; not worth it now)
Full gamma curve (p1 0x6e–0x83), auto-contrast (p1 0x1d), black level (p0 0xf0–0xf3), lens-shading grid —
each is a many-register block; adding them all would consume far more write budget than available and is
not worth the risk for a kids' camera. Documented for completeness.

## Key caveats (verify before tuning sensor registers)
- **Page attribution:** the init's first 31 writes happen BEFORE any `0xfd` page-select, i.e. on the sensor's
  power-on-default page, and several of those addresses (0xba/0xbb/0x80/0x43/0xda) are page-1/2 functions in
  the sibling drivers. So our dump's page tags are only reliable AFTER the explicit selects (`0xfd` = 0x20,
  0, 0x1d, 1 in order). For any sensor-reg change, confirm the active `0xfd` at that write first.
- **AWB/print overlap:** the AWB patch already repurposes the 5 trailing page-1 writes (incl. 0x56, 0xb4).
  A new AE/sharpness repurpose must use a DIFFERENT run of sacrificable writes, or be merged with AWB.
- **Streaming power-sequence:** the esp driver notes SP0A39 has no soft-standby and relies on the hardware
  PWDN pin; if any of these changes make streaming flaky, that power sequencing is where to look.

## Suggested next step
#1 (AE target brightness) is the biggest image win and is deliverable with the proven AWB-style
trailing-write repurpose. If you want it, the build is: find a sacrificable page-1 write run, map it to
`0xfd=1; 0xcb=<target>`, offline-validate the emitted sequence (as we did for AWB), Codex-verify, ship as a
tunable `ae-target` command. Everything else is lower payoff or needs on-device tracing first.
