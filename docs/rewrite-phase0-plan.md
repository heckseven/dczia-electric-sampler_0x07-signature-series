# Rewrite, Phase 0: ground truth

Measurement only. No specification is written and no rewrite code is committed until
these numbers exist. Produced by an architect pass and two adversarial critiques; the
second returned PROCEED WITH AMENDMENTS and its five blocking items are folded in below.

## Constraints

- **No bench instruments, no human in the loop.** Every measurement is taken by firmware
  that measures itself and prints over USB serial, driven by a host script that flashes,
  captures, and recovers the badge when it wedges.
- **Latency target is "best achievable"**, not a fixed number. No success criterion may
  be satisfiable by only one outcome.
- One badge. **The badge is a mutex**: these are dependency-parallel, not
  wall-clock-parallel.
- **Exactly two session transitions.** Task 1 (host half), Task 3 in CircuitPython, one
  transition to bare-metal C, Tasks 2/4/5/6/7, one transition back.
- New branch, `Spikes/`. `heckseven/sampler-rework` and `main` are untouched.
- Cortex-M0+: no SMULL, no saturating arithmetic, 8 usable low registers, **no DWT cycle
  counter** - SysTick at `clk_sys` is the cycle source, 24-bit, ~126 ms wrap.
- GP19 is the only free GPIO. GP7 carries the firmware's own 5 ms sync pulse.

## Task 1 - Self-recovering harness (prerequisite; gates 3-7)

Host: port discovery, Ctrl-C, paste-mode REPL, reset-and-wait-for-re-enumeration, the
CIRCUITPY write-protect cure, `Tools/build.py` deploy, and both transitions -
`microcontroller.on_next_reset(RunMode.BOOTLOADER)` out of CircuitPython,
`picotool reboot -f -u` out of C. One line-oriented `KEY=VALUE` report format.

**Amendments:**
- **Scratch registers 0..3 only.** The SDK reserves `WATCHDOG_SCRATCH4..7` for its
  watchdog-reboot magic - a case index written there makes the next watchdog reset jump
  to a garbage vector, which is an unrecoverable boot loop no host and no picotool can
  reach, because the board never enumerates. Guard with a magic word plus a checksum.
- **The watchdog covers one failure mode, not four.** It catches a hang with the CPU
  alive, including with interrupts disabled. It does *not* catch TinyUSB dying while the
  main loop keeps feeding the dog - a badge that is healthy, silent, and never resets,
  with `picotool` needing the very USB that is gone. So: feed the watchdog from exactly
  one timer ISR that *also* requires a host heartbeat byte within N seconds, making
  "USB dead, CPU alive" a reset. And open every C spike with a fixed ~3 s CDC-up window
  before any peripheral init, giving a guaranteed re-entry point after a host crash.
- **Per-case host timeout**, so a non-responding badge is declared unrecoverable within
  a bounded time rather than hanging the campaign.

**Success:** a fault matrix - spin with IRQs on, spin with `__disable_irq`, a `udf` hard
fault, and a deliberate TinyUSB teardown - each either recovered unattended, or
documented as needing one hands-on BOOTSEL with that residual stated up front.

## Task 2 - Minimal C scaffold and TinyUSB fixed overheads (prerequisite; gates 4-7)

Session: C. Depends on 1.

One pico-sdk scaffold: both cores, CDC reporting, SysTick as cycle source, Task 1's
recovery contract, picotool reset-via-vendor-interface. Link three variants and report
**fixed overheads only** - vector table, `.data`, `.bss`, per-core stacks, heap
high-water - from the linker map plus a runtime probe. Variants: (a) CDC only,
(b) + TinyUSB MIDI, (c) + TinyUSB MSC + FatFS + SD block driver. Attribute each delta to
named buffers. Record PIO program **instruction counts** (32 slots per block, shared
across 4 SMs - instructions bind before state machines do) and an SM allocation for
I2S + NeoPixel.

Do **not** initialise nine peripherals. Do **not** compute a sample budget - later
phases subtract from these overheads.

**Amendment:** variant (a) is the mandatory common base every later spike compiles
against. (b) and (c) are overhead measurements with no downstream consumer - Task 6 in
particular wants raw DMA'd SPI with no filesystem, which contradicts (c).

**Success:** three-row table of fixed SRAM overhead by named region, each delta
attributed to specific buffers, whatever the totals are.

## Task 3 - CircuitPython baseline: what the current firmware actually does

Session: CircuitPython. Depends on 1. **This is the richest task and it is one run.**

Instrumented fork, host-driven over USB MIDI (`python-rtmidi`/ALSA to the badge's own
port) so >=500 trials run unattended, with the host's own send-to-echo round trip
recorded as an outer bound.

**Amendments - four additions, all one instrumented run:**
1. **Establish the timebase first.** `time.monotonic_ns()` does not have nanosecond
   resolution on this port; it is the 1024 Hz raw tick plus subticks. Measure its
   resolution and monotonicity before quoting any stage to a precision the clock does
   not have.
2. **Name the USB MIDI poll as its own stage, and run a second configuration with
   `USB_MIDI_POLL_MS = 0`.** `sequencer.py:260` sets it to 20 ms and `sequencer.py:1322`
   gates the drain on it - so the stimulus path carries a 20 ms quantiser the pad path
   does not have, and without this the headline result is predetermined and about a
   stage that does not exist in the rewrite. Note that per-pass polling "triples the
   loop period" (`sequencer.py:237`), so report both configurations.
3. **Main-loop pass-duration histogram.** Bucketed counters, no allocation. This is what
   gates how fast anything can be serviced, and the repo's own numbers disagree by 34x:
   `sequencer.py:237` says "around 200 us", `handoff-2026-08-28.md:171` measures ~125
   passes/s (8 ms), `streaming-bug-rootcause.md:706` measures 6.8 ms. Reconcile them
   explicitly. p50/p99/max.
4. **Mixer `buffer_size` sweep downward: 1024/512/256/128.** `sequencer.py:480`
   constructs the Mixer with no `buffer_size`, taking the 1024-byte default - 32 ms at
   16 kHz. Enlarging it was refused; nothing has ever tried shrinking it, and that curve
   is the input the rewrite's block-size requirement is written against. Report
   underruns and `audio_errors` per size.

**Pin the load condition:** transport running, four-track pattern, display refreshing,
LEDs animating. On an idle badge the tail is fiction. Report collect count, minimum
`gc.mem_free()`, and a GC pause histogram alongside the per-stage table.

Also read back at runtime - not from upstream source - the keypad scan interval and
debounce threshold, the `audio_dma` block size, and the mixer buffer depth. The badge
runs a DCZia-supplied `.uf2` and nothing records whether it is stock 10.2.1.

**Not measurable here:** the pad's mechanical contact-to-event time. No hands.

**Success:** per-stage table, min/mean/worst over >=500 unattended trials under the
pinned load, in both poll configurations; the four additions above; and one sentence
naming which stage dominates - whichever it is.

## Task 4 - Mixer cost and SRAM bank contention

Session: C. Depends on 2.

Fixed-point mixer, samples in SRAM, SysTick-timed over 1-24 voices. Four inner-loop
variants: no interpolation; linear; linear via the **SIO interpolator peripheral**; and
XIP-flash-resident as a comparison (cold and warm cache, 18 voices at scattered
addresses). Treat ~10 cycles/voice-frame as optimistic until measured.

Contention: mixer on core1 against live I2S DMA plus a second DMA channel, sample data
placed (a) in the same 64 KB striped bank as the DMA target, (b) a different striped
bank, (c) `SCRATCH_X`/`SCRATCH_Y`. Report the cycles/frame delta.

**Amendment:** Task 4 delivers **one parameterised `spike_audio.c`** - I2S PIO + DMA +
the mixer inner loop, variant and placement selectable - which Task 5 consumes
unmodified. Otherwise three independently built mixers produce numbers that cannot be
compared, and Task 5(b)'s underrun counts are meaningless without knowing which variant
and placement produced the load.

**Success:** cycles-per-frame across voice count x variant x placement, converted to
worst-case CPU fraction at 16 kHz, plus a placement recommendation - including if
placement makes no measurable difference, or if the budget exceeds one core.

## Task 5 - Output-path latency floor and the flash/XIP hazard

Session: C. Depends on 2 **and 4**.

**(a) Trigger-to-output.** Amended twice:
- **Dither the trigger instant uniformly across the block period.** A self-trigger at a
  fixed SysTick phase sits at a fixed offset from the DMA completion IRQ, so 10,000
  trials repeat one number and min/mean/worst collapse - a degenerate result wearing a
  distribution's clothes.
- **Measure the pin, not the FIFO.** The DMA transfer count is real hardware, but it
  says when a frame entered the PIO TX FIFO, not when it left. The 4-8 deep FIFO and the
  PIO program's serialisation are unmeasured. Fix with no wire and no free GPIO: a
  **second PIO state machine reads GP0/GP1/GP2 as inputs while the I2S SM drives them**,
  waits for the first non-zero data bit and raises a PIO IRQ - a true hardware timestamp
  of the sample leaving the pin.

Sweep DMA block sizes 16/32/64/128 frames.

**(b) Flash hazard.** With audio code and data in SRAM (`__not_in_flash_func`) and core1
parked per the SDK's flash-safe protocol, erase+program a 4 KB sector while the mixer
runs; count underruns and the longest inter-refill gap, sweeping pre-fill depth.

**Amendment - name the address.** The erase target must be a **reserved top sector
(0x101FF000) declared via a linker section**, with a runtime assert that the target is
`>= __flash_binary_end` and inside that sector, refusing to run otherwise. An erase
landing in the loaded image bricks the only badge into a state whose only recovery is
holding BOOTSEL while replugging - two hands, which is the one thing this campaign
cannot ask for.

**(c)** MAX98357A group delay and shutdown-wake/pop-suppression ramp, from the
datasheet, tagged derived not measured. The wake-up ramp is what makes
"stop the stream when idle" expensive.

**Success:** trigger-to-output frame count min/mean/worst per block size with the phase
dithered and the pin instrumented; a pre-fill depth that makes flash writes
underrun-free, or an explicit finding that none does; the two amp figures with
provenance.

## Task 6 - Storage timing

Session: C. Depends on 2 (base variant (a), plus a raw SPI driver - not variant (c)).

DMA-driven SPI at 24/16/8 MHz, no filesystem yields, single-block reads into a RAM ring
dumped over CDC, 60 s per card per clock. Then the quantity the RAM-resident design
actually needs: **time to load a full kit** - sequential throughput filling 32/64/128 KB,
a boot-time requirement rather than a real-time one. Confirm whether the throwaway first
read after init and the 22-bit `C_SIZE` handling are still needed with a C driver.

**Amendment - drop the p99 trigger.** The hazard is a rare card GC stall of hundreds of
milliseconds, plausibly arriving less than once per 60 s, so a clean 60 s p99 licenses
nothing about the tail. Keep only the sound trigger: run the extended campaign **if and
only if a later phase selects streaming**. State plainly that the 60 s run characterises
the typical case and is not a bound on the tail.

**Success:** p50/p99/max per card per clock over 60 s, kit-load time per KB, and the
workaround verdicts.

## Task 7 - Display cost, and the supply-coupling artefact

Session: C. Depends on 2.

**Amendment - a free digital null test first.** Whether the pop is analog at all is
currently a hypothesis from a comment at `sequencer.py:191` confirmed by ear. Capture
the mixer's own output words with the display bus idle versus hammering: identical
output proves the artefact is downstream of the digital path and makes the load-step
budget the right substitute rather than an assumed one. If they differ, there is a
software cause and it is fixable.

Then time a full 512-byte SSD1306 frame and a partial-window update over a DMA'd C I2C
driver at 400 kHz and 1 MHz, in microseconds, **CPU occupancy separated from bus time**.
The current 32 ms is a `displayio` figure and the 400 kHz bus floor is ~11.5 ms.

The artefact itself **cannot be measured by instrumented firmware** - there is no
on-badge sensor, and the RP2040 ADC's reference derives from the same 3V3 rail, so it
cancels exactly the quantity of interest. Substitute: a datasheet load-step budget over
the SSD1306 panel, ten NeoPixels at `brightness=0.1` and the MAX98357A against the Pico W
regulator's transient spec, naming the dominant contributor. Then the software
mitigations that makes available, and the fallback if unresolved - a runtime-selectable
stop-stream-when-idle mode, priced with Task 5's amp wake-up figure.

**Success:** the null test result; measured frame times at both speeds with CPU
occupancy separated; the load-step budget; and a written disposition of the pop as
software-mitigable or as the one deferred item needing a manual measurement, with the
cost of deferring stated.

## Deliberately absent

**Pad contact bounce.** It cannot be measured without someone pressing pads. The
engineering answer that removes the need is asymmetric debounce - fire on the first
edge, debounce only the release - under which bounce duration governs retrigger
suppression rather than latency. But it still gates whether one strike makes one note or
two: **the release window must exceed the make-bounce duration.** 8 ms is assumed and
unverified, must be a runtime constant so it can be corrected without a rebuild, and the
firmware should log bounce histograms passively as telemetry during normal play.

**Per-strike velocity.** Already answered: `setup.py:110` is a plain digital
`KeyMatrix`. Per-step velocity exists separately and is unaffected.

**Scope work, manual pad strikes, differential probes.** Excluded by constraint.

## Residual risk

**Correction, found by using it: `Firmware/DCZiaSampler.uf2` is not the way back.**

It is a 2023 image - `Adafruit CircuitPython 8.2.2 on 2023-08-01` - carrying DCZia's
stock firmware as loose `.py` files. Flashing it does three things nobody wanted:

- downgrades CircuitPython by two major versions, so the rework will not run at all
  (it needs `i2cdisplaybus`, which 8.x does not have),
- replaces the deployed build with stock DCZia sources, which then shadow anything
  deployed over the top, and
- spans the whole 2 MB, so it rewrites the CIRCUITPY filesystem, where the 28 KB spike
  image writes only the bottom of flash and leaves it alone.

The real return path is three steps, all host-driven:

1. `Spikes/host/flash.py Firmware/circuitpython-10.2.1-pico_w.uf2` - stock
   CircuitPython 10.2.1 for `raspberry_pi_pico_w`. Verified by the banner matching what
   the badge ran before. Reproducible without the file:

   ```
   https://downloads.circuitpython.org/bin/raspberry_pi_pico_w/en_US/
       adafruit-circuitpython-raspberry_pi_pico_w-en_US-10.2.1.uf2
   sha256  df4edd8a783d0014ef276398c94a417c07f33677c9417326dd14c4ea5c659ce3
   ```

   The image is 2,927,616 bytes and is untracked - a 2.8 MB binary is the user's call to
   commit, and the URL plus checksum is enough to get it back either way.
2. Clear the volume. `Tools/build.py` only ever copies, so stale stock modules survive
   a deploy and shadow the build.
3. `Tools/build.py -o <CIRCUITPY> --mpy-cross ./mpy-cross`.

Songs, kits and `settings.prefs` are on the SD card and survive all of it.

Rollback is host-driven **only while the image is alive**. `picotool reboot -f -u` reaches BOOTSEL from a running image that
still exposes the reset interface - exactly what a wedged spike may not. A badge that
wedges below that level needs one hands-on BOOTSEL. Task 1's fault matrix is what bounds
how often that happens; it cannot make it never.

The golden-vector corpus (a later phase) depends on the current Python remaining
runnable to generate vectors from, so the campaign must leave the badge able to return
to CircuitPython. Same residual.
