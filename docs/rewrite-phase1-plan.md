# Rewrite, Phase 1: the audio core, playable

Phase 0 measured the ground. Every number the original plan estimated has now been taken
on the badge, and several of them moved. This phase is written against the measurements,
not the estimates.

## What Phase 0 settled

| quantity | estimated | **measured** |
|---|---|---|
| free SRAM, C | "roughly 8x" | **252,680 B (246.8 KB), 8.4x** |
| mixer, 18 voices interpolated | ~2% of a core | **7.2%** (7.5% under DMA contention) |
| cycles per voice-frame | ~10 | **31.3** |
| trigger-to-output | unknown | **block x (1..2) + 568 us** |
| flash erase, audio alive | feared fatal | **35 ms, zero underruns** |
| SD block read | unknown | **476 us mean, 1.3 ms worst** |
| SD sequential | unknown | **1,524 KB/s** |
| display frame | 32 ms (displayio) | **12.78 ms bus, 38 us CPU** |
| amp wake from standby | unknown | **7 ms typ** |
| system clock | 133 MHz | **125 MHz** |

## Decisions those numbers force

**32-frame blocks.** 4.56 ms worst-case trigger-to-output, half the 10 ms budget, leaving
room for the mixer and for the TX FIFO to stay joined. 16 frames is available later if
something needs 2.56 ms.

**The I2S stream never stops.** The amp takes 7 ms to come out of standby - 73% of the
budget - so idling means streaming silence, not stopping BCLK. This reverses what the
CircuitPython firmware does, and Phase 0 is why.

**The whole audio path lives in SRAM.** `__not_in_flash_func` on the ISR and everything it
calls, no calls into flash from the ISR, and every interrupt but the audio DMA masked
during a flash write. Measured: a 35 ms erase costs zero underruns and no measurable
jitter. This is what makes saving during playback safe, and it is not available in
CircuitPython at all.

**No allocator in the audio path.** Samples live in one static arena sized at build time.
The failures in `streaming-bug-rootcause.md` were allocation and collection, not
throughput - the mixer has 13x headroom.

**Scratch banks are not worth using.** Moving sample data to `SCRATCH_Y` bought 0.5
percentage points. Not a tool worth complexity.

## Scope: LIVE mode only

Phase 1 is a vertical slice that proves the architecture end to end on hardware and is
playable. Everything else waits.

**In:** boot, I2S out, mixer with per-voice pitch, kit load from SD, pad input, display,
volume. **Out:** sequencer, song format, save/load, menus, MIDI, HID, animation, sync.

Excluded deliberately, not forgotten: reproducing 31 modules of working behaviour is the
real cost of this rewrite, and it should not begin until the core it depends on is proven
on the badge.

## Success criteria

Each is measurable by the Phase 0 harness, unattended:

1. Boots to playable in under two seconds, kit loaded from the card.
2. Twelve pads trigger samples. Trigger-to-output **at or under 5 ms worst case**,
   measured at the pin by the Task 5 method.
3. Per-track pitch works over at least +/- one octave and is audibly correct.
4. **Zero underruns over ten minutes** of continuous play at 16 voices.
5. No allocation after init - provable by the absence of `malloc` in the linked image.
6. Display updates via DMA'd partial windows without perturbing audio: the Task 7 null
   test still returns `identical=1`.
7. Free SRAM at the end of init is reported, and the sample arena is at least 180 KB.

## Order of work

1. **Skeleton and pin map.** One header, from `setup.py`, so no pin is ever guessed.
2. **Audio core.** I2S PIO, ping-pong DMA, mixer on core 1. Silence first, then a tone,
   then samples. This is the highest-risk piece and it goes first.
3. **Storage.** Raw SD block driver at 20.8 MHz plus a read-only FAT32 reader. No FatFS:
   reentrancy is what broke the Python, and Phase 1 only needs to read.
4. **Input.** 3x4 key matrix and two encoders, polled on core 0.
5. **Display.** DMA'd SSD1306, partial windows.
6. **Bring it together**, then re-measure criteria 2, 4 and 6 on hardware.

## What could still go wrong

The tail of the SD card. Phase 0 characterised the typical case over six minutes and
explicitly did not bound a rare garbage-collection stall. Phase 1 loads kits at boot,
which is not real-time, so this does not bite yet - but it is the open question standing
between "6 seconds of sample" and "as long as the card".
