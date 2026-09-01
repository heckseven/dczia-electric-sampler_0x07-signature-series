# The streaming bug: root cause and options

> **Status: Option A is implemented.** Streaming is gone from the firmware -
> `WaveFile`, the per-track file handles and the read buffers with it. Samples
> that do not fit their share are loaded head first and faded out, and
> `Tools/convert_samples.py --max-seconds` trims a kit properly offline. The
> shipped kit is trimmed to 46,340 of RAM_BUDGET's 49,152 bytes. Section 2's
> options B, C and D remain open, and section 6 remains the answer on leaving
> CircuitPython.

Follows `docs/handoff-2026-08-28.md`, which left one item open: a streamed
sample plays exactly once, and the second hit leaves the voice silent and the
file handle spent with nothing raised.

That is now explained, from the CircuitPython 10.2.1 sources, and it matches
every number in the handoff. Not upstreamable as-is; drop before any PR to the
DCZia repo.

---

## 1. What actually happens

The chain, each link read in the 10.2.1 tree:

**1. Every SD byte yields to the audio refill.**
`ports/raspberrypi/common-hal/busio/SPI.c` spins `RUN_BACKGROUND_TASKS` inside
*both* transfer loops - the DMA wait and the software FIFO loop. `sdcardio`
drives the card entirely through `common_hal_busio_spi_read/write`
(`shared-module/sdcardio/SDCard.c`). So an SD read is not atomic: it yields,
repeatedly, mid-transfer.

**2. The audio refill is a background callback.**
`ports/raspberrypi/audio_dma.c`: `isr_dma_0` does not load the block itself, it
calls `background_callback_add(&dma->callback, dma_callback_fun, dma)`.
`background_callback_run_all()` is reached from `RUN_BACKGROUND_TASKS`
(`supervisor/shared/background_callback.c`). The refill therefore runs
*inside* step 1's wait loop.

**3. The refill re-enters the filesystem.**
`dma_callback_fun` -> `audio_dma_load_next_block` -> `audiosample_get_buffer`
on the Mixer -> `mix_down_one_voice` -> `audioio_wavefile_get_buffer` ->
`f_read` on the streamed WaveFile - on the same volume, down the same SPI bus
that is mid-transfer one frame up the stack. FatFS is not reentrant and the
`fs->win` sector window is shared.

**4. The resulting error is latched into the file, permanently.**
FatFS aborts the operation and stores the code in `fp->err`. Every later
`f_read` *and* `f_lseek` on that handle returns it immediately without moving
`fptr`. That is exactly the handoff's evidence: position frozen at 556,
`seek(0)` a silent no-op, `read` raising `OSError(5)`. Only close-and-reopen
clears it. (Standard ChaN FatFS contract - the one link here I inferred from
behaviour rather than read, because the vendored `ff.c` would not fetch.)

**5. Nothing reports it.**
`shared-module/audiomixer/Mixer.c`, `mix_down_one_voice`, has no
`GET_BUFFER_ERROR` branch:

```c
if (voice->buffer_length == 0) {
    if (!voice->more_data) {
        if (voice->loop) { audiosample_reset_buffer(voice->sample, false, 0); }
        else { voice->sample = NULL; break; }          // <- silent death
    }
    if (voice->sample) {
        result = audiosample_get_buffer(..., &voice->buffer_length);
        voice->buffer_length /= sizeof(uint32_t);
        voice->more_data = result == GET_BUFFER_MORE_DATA;
    }
}
```

And `audioio_wavefile_get_buffer` returns `GET_BUFFER_ERROR` *without writing*
`*buffer_length`. So the caller keeps a stale length, `more_data` goes false,
the next pass takes the `else` and sets `voice->sample = NULL`. Since
`common_hal_audiomixer_mixervoice_get_playing` is just `sample != NULL`, the
track reads as not playing, no exception crosses into Python, and
`audio_errors` stays 0.

### Why the second hit specifically

`trigger()` calls `voice.play(sample)`, which is
`common_hal_audiomixer_mixervoice_play`:

```c
self->sample = sample;                                  // set FIRST
audiosample_reset_buffer(sample, false, 0);             // f_lseek
result = audiosample_get_buffer(sample, ...);           // f_read -> SD -> yields
```

The first hit is safe: nothing is pulling that file yet, so the yield inside
its `f_read` finds no reader for it. The second hit is not: the DMA is now
actively pulling this WaveFile, `self->sample` is already set, and the `f_read`
that `play()` itself performs yields straight into a refill that reads the same
handle. That is the collision.

It is not really "the second play". It is **any SD access while a streamed
voice is sounding** - which also covers `audition()`, `load_track()` from the
browser, and song/kit loads.

### Every measurement in the handoff, accounted for

| observed | explanation |
|---|---|
| `tell=44` -> `15916` -> `556`, then frozen | 556 = data_start 44 + one 512-byte half-buffer. `STREAM_BUFFER=1024` is halved by `WaveFile` (`self->len = buffer_size / 2`). So `play()`'s own reset+read succeeded; every read after it failed and left `fptr` untouched. |
| nothing raises, `audio_errors` 0 | step 5 - `voice->sample = NULL` with no error path |
| seek "ok" but `tell` unchanged | latched `fp->err`; `f_lseek` returns early |
| retrigger @0.5s on a 1.16s sample: dies | overlap guaranteed - the voice is always still pulling |
| retrigger @1.5s (finishes first): 1/20 | no overlap, so only the residual window |
| RAM sample @0.5s: 0/40 | `RawSample.get_buffer` touches no file and no SPI |
| rebuild WaveFile per hit: 12/30 | the *old* WaveFile is still attached and being pulled; the constructor's own `f_read`s collide with it |
| also reopen per hit: still 12/30 | same reason - reopening the handle does not detach the old one |
| 13.8 ms per trigger | SD reads dragged into the audio path |

The handoff's "may not be fixable from Python" is correct, and now has a
reason: `play()` sets `sample` before it does its file I/O, and Python has no
way to hold off background callbacks.

---

## 2. Options

### A. Delete streaming - everything plays from RAM

Any sample too big for `MAX_RAM_SAMPLE` loads its first N bytes with a short
fade instead of streaming. `Tools/convert_samples.py` gains `--max-seconds`
so the shipped kit is trimmed properly offline; runtime truncation is the
safety net for anything the player picks off the card.

- Removes file I/O from the audio path completely. The one measured
  configuration that never failed.
- Deletes `_files`, `_stream_buffers`, `_streamed`, and the whole streaming
  branch of `_load_one` - a real simplification.
- Cost: 24 KB is 0.77 s at 16 kHz. The open hat (1.16 s) and cymbal (2.06 s)
  get shortened. Raising the budget does not rescue them: 48 KB of budget
  against ~86 KB free, and kick+snare already take 28.8 KB.
- Lowering the mixer rate buys length but spends it in the treble, which is
  the wrong currency for hats and cymbals.

### B. Stream from internal flash, never from the card

`supervisor_flash_read_blocks` is a `memcpy` out of the XIP-mapped region -
no SPI, no `RUN_BACKGROUND_TASKS`, so a refill from flash cannot re-enter
anything. Long kit samples move to CIRCUITPY and stream from there; the card
keeps the browsable library, and anything picked off the card goes through A's
RAM path.

- Keeps the shipped cymbal at its full 2.06 s.
- Flash has 252 KB free on the bytecode build; the two long samples are 103 KB.
- Flash also sustains 391 KB/s against the card's 169 KB/s in small reads.
- Cost: two sample locations to reason about, and a deploy-size ceiling. Needs
  A as the fallback anyway, so it is A plus a fast path - not instead of it.

### C. Python-side mitigations only

`voice.stop()` before `voice.play()` on a streamed track; refuse a retrigger
while `voice.playing`. Narrows the window - `stop()` sets `sample = NULL` so
the mixer will not pull during the reset - but `play()` re-arms `sample`
*before* its own `f_read`, so the hole is smaller, not closed. Cheap, and not
sufficient on its own.

### D. Patch CircuitPython

`Firmware/DCZiaSampler.uf2` already ships, so a custom build is not exotic here.
Two independent patches:

- **D1** - give `mix_down_one_voice` a `GET_BUFFER_ERROR` branch that stops the
  voice *and* records something Python can read. Small, self-contained, and
  upstreamable. Turns a silent dead track into a reportable fault whichever
  other option is taken.
- **D2** - the root-cause fix: `background_callback_prevent()` /
  `background_callback_allow()` around the sdcardio block operations, so the
  audio refill cannot fire inside an SD transfer. A 512-byte read at 333 KB/s
  is ~1.5 ms against a 32 ms audio buffer, so the deferred refill has ample
  margin. This is what would make streaming from the card actually correct.

### E. Detection and recovery - worth doing regardless

Nothing currently notices a dead track. After a trigger on a streamed track,
or on a periodic sweep, compare `voice.playing` against expectation; on death,
close and reopen the handle (the only thing that clears `fp->err`) and count
it. Cheap, and it converts a silent failure into a visible one.

---

## 3. Recommendation

**A + E** as the floor: it is the only configuration measured at zero failures,
it deletes code rather than adding it, and it makes the remaining failure mode
visible. Add **B** if the full-length cymbal matters - it is a fast path on top
of A, not a second mechanism. Take **D1** whenever a firmware build is next cut;
it is small and belongs upstream. **D2** only if streaming from the card is
genuinely wanted, and with the understanding that it means owning a fork.

**C alone is not enough** and should not be mistaken for a fix.

---

## 4. Also confirmed: the idle collection rate

The handoff's suspicion was right. `SamplerState._render_idle_pixels` runs
every `IDLE_FRAME_MS` (25 ms, so 40 fps) and calls an animation function that
allocates unconditionally: `_blank()` is `[OFF] * PIXEL_COUNT`, a fresh
ten-element list per frame, plus a tuple per lit pixel from `wheel()` and
`dim()`. The active path is gated behind `_pixels_dirty`; the idle path is not
gated at all, because "the animation moves on its own, so nothing marks it
dirty".

Fix shape: keep a preallocated frame buffer and have the animations write into
it, or gate the idle frame on the tick actually changing. The *upward trend*
in the rate is still unexplained by this and is more likely fragmentation.

---

## 5. Revised scope, and a correction that shrinks the defect

The requirement is narrower than section 2 assumed: a player swaps samples in a
kit or loads more in. That happens at load time - most likely at boot, while
restoring the configured kit - and never during playback.

**Correction, from `supervisor/shared/background_callback.c`.**
`background_callback_run_all()` increments `background_prevention_count` before
running callbacks and returns immediately if it is already non-zero. So when a
refill's own SD read spins `RUN_BACKGROUND_TASKS` inside the SPI loop, the
nested call is blocked. **A refill cannot re-enter a refill.**

The hazard is strictly one-directional: *main thread touches FatFS/SPI ->
yields -> refill re-enters FatFS*. That is much narrower than "any SD access
while streaming", and it has two consequences:

- **Loading a kit with nothing playing was never the broken path.** It needs no
  fix. The section 2 options were written against a bigger problem than exists.
- **D2 is not a new invention.** It extends, in the other direction, a guard
  CircuitPython already applies. That makes it a considerably easier sell than
  section 2 implied.

What remains is `voice.play()` on a streamed WaveFile, which does main-thread
SD I/O and can collide with a live stream. Triggering a RAM-backed track is
always safe - `RawSample.get_buffer` touches no file.

### The decision is now capacity, not correctness

At 16 kHz mono 16-bit, one second of audio costs 32 KB.

| store | budget | per track, 8 tracks | safe during playback |
|---|---|---|---|
| RAM | ~70 KB usable of ~86 KB free | ~8.75 KB = **0.27 s** | always; no I/O in the audio path |
| CIRCUITPY flash | ~252 KB free | ~30 KB = **0.95 s** | yes; flash reads are a `memcpy` from XIP and never yield |
| SD card | the whole library | unlimited | only with D2 |

0.27 s covers kick, snare, rim, clap and closed hat. It does not cover a crash,
ride or open hat.

**Recommendation: A as the foundation, B layered on only if long sounds
matter.** A alone satisfies the requirement and is the only option that makes
the defect unreachable rather than merely avoided. B's two objections - the
audio pause during flash writes, and write timing - do not apply when loading
happens at boot with nothing playing; its one live cost is USB ownership, since
`boot.py` claiming the filesystem makes CIRCUITPY read-only to the host and
breaks `Tools/build.py -o /media/<you>/CIRCUITPY`. Gate that behind a dev-mode
key at boot, or a "kit dirty" flag on the card so only boots that need it claim
the filesystem. Sync after writing: a dirty sector cache turns a later flash
*read* into an erase+program, and that pauses audio.

---

## 6. Would leaving CircuitPython help?

Researched separately. Short answer: **the defect would not survive a
migration, but that is not a reason to migrate.**

Three of the four links are CircuitPython architecture; one is universal.

| link | CircuitPython-specific? |
|---|---|
| SPI transfer spins `RUN_BACKGROUND_TASKS` mid-transfer | yes - MicroPython's `ports/rp2/machine_spi.c` has no event-poll hook at all |
| audio refill is a deferred callback that reads the file itself | yes - MicroPython's `machine_i2s.c` IRQ only does `ringbuf_pop()` and memsets silence on underflow |
| `mix_down_one_voice` swallows `GET_BUFFER_ERROR` | yes - nobody else has `audiomixer` |
| FatFS latches `fp->err` | **no** - ChaN behaviour, inherited everywhere |

Every other runtime avoids the class the same way: **file reads happen on the
main thread and the DMA side touches only RAM.** That is exactly what options A
and B do *inside* CircuitPython - which is why the rewrite buys nothing here.

**Cost of leaving:** 7,735 lines of production Python and 11,871 lines of tests,
against a CircuitPython-only surface of `displayio`, `keypad`, `rotaryio`,
`neopixel`, `usb_midi`, `usb_hid`, `storage`, `sdcardio`, `audiomixer`,
`audiocore`, `audiobusio`, plus the Adafruit MIDI/HID/display libraries.
MicroPython has none of these and **no mixer** - 18-voice 16-bit mixing would
have to be written in C anyway, so it is the worst of both: rewrite everything
and still maintain C, without the current testing story.

**The one argument with real force is RAM**, and it points at C, not
MicroPython:

| runtime | free RAM | total kit audio at 16 kHz |
|---|---|---|
| CircuitPython 10.2.1 (measured on the badge) | ~86 KB with the engine loaded | ~1.5 s |
| MicroPython | ~161-187 KB at boot | ~3.8 s |
| Pico SDK C / Rust | ~264 KB less BSS and stack | ~6 s *(inference)* |

If 8 tracks of full-length samples is a product requirement, that is a genuine
argument. It is not an argument about this bug.

**Upstream status:** not fixed. [#7322](https://github.com/adafruit/circuitpython/issues/7322)
(audio glitches when an I2C or SPI display updates) is open since 2022 on a
"Long term" milestone with no analysis; [#7856](https://github.com/adafruit/circuitpython/issues/7856)
and [#289](https://github.com/adafruit/circuitpython/issues/289) are the same
family. Nothing names the `RUN_BACKGROUND_TASKS` SPI reentrancy or the
swallowed `GET_BUFFER_ERROR`, so **D1 and D2 look like novel, upstreamable
contributions rather than duplicates.**

**RP2350 / Pico 2** would largely dissolve the RAM constraint for a future board
revision - 520 KB SRAM lets an all-RAM kit hold everything at full length, so
nothing needs to stream. CircuitPython's RP2350 audio support has been rough
([#9517](https://github.com/adafruit/circuitpython/issues/9517)). Not a fix for
this badge; a real answer for the next one.

### A free win: the `C_SIZE` truncation is a one-line upstream bug

Verified in `shared-module/sdcardio/SDCard.c:319`:

```c
if (csd_version == 1) {
    self->sectors = ((csd[8] << 8 | csd[9]) + 1) * 1024;   // 16 bits
}
```

CSD v2.0's `C_SIZE` is 22 bits, spanning `csd[7][5:0] | csd[8] | csd[9]`.
CircuitPython drops `csd[7]` entirely. MicroPython's driver gets it right:

```python
self.sectors = ((csd[7] << 16 | csd[8] << 8 | csd[9]) + 1) * 1024
```

This reproduces the handoff's arithmetic exactly (0x1DD7F -> 0xDD7F, the low 16
bits). Fixing it upstream retires the sector-58,000,000 partitioning workaround
and makes cards over 32 GB simply work. Worth taking regardless of every other
decision here.

---

## 7. Deployed and measured on the badge, 2026-08-29

Bytecode build of the working tree, `Tools/build.py -o <CIRCUITPY> --mpy-cross ./mpy-cross`.
31 compiled, 33 copied; 158 KB used of 490 KB, against 239 KB before - the
trimmed kit is most of the difference.

**Boot:** one USB re-enumeration from the reset, then `main.py` for 96 s with
no `Traceback`, no error and no safe mode. A second clean run after a Ctrl-D
reload.

**What the kit actually loaded.** The card mounted this boot, and
`SAMPLE_DIRS` puts `/sd/samples` first - so the badge loaded the *untrimmed*
originals off the card, not the trimmed copies in flash. That is the runtime
trimming doing exactly its job:

| track | sample | file | loaded | seconds | trimmed |
|---|---|---|---|---|---|
| 0 | kick_crater | 20,812 | 12,288 | 0.384 | yes |
| 1 | snare_kraken-head_1 | 7,984 | 7,940 | 0.248 | no |
| 2 | hh_hats-open_1 | 37,064 | 14,462 | 0.452 | yes |
| 3 | cymbals_crucible-edge_1 | 66,034 | 14,462 | 0.452 | yes |

`ram_used` 49,152 of 49,152, and `has_sample` true on all four.

This is what forced the budget to be **shared across a kit rather than served
first come**. With first come, tracks 0-2 spent the whole budget and the
cymbal was silent - reintroducing the exact failure holding samples in RAM was
meant to remove. `_allowance` now divides what is left by the tracks still to
load.

**The retrigger that used to kill a track.** 40 iterations, both long tracks,
every 500 ms - the demo pattern's timing against a sample longer than the gap:

```
RESULT fails=0 silent=0 audio_errors=0 last=None
after: triggers still work -> True True True True
```

Against the streamed measurement in the handoff - 1/40 failed to start, and
the track dead within seconds - and checking `voice.playing` 50 ms after every
one of the 80 hits.

**Both new settings, on the real card:** `prefs.set_animation('Sweep')` wrote,
read back, and restored; `view.centred('HECKSEVEN', 21)` returned six leading
spaces, which is where the middle of a 21-column row is.

### One hardware fact worth the next person's hour

**CIRCUITPY can come up write-protected, and no remount will move it.**
`/sys/block/sda/ro` reads `1`, the mount is `ro`, and `udisksctl unmount` +
`mount` changes nothing - the kernel caches the SCSI write-protect bit from
device attach. Meanwhile the badge itself gets `OSError 30` writing to `/`, so
*both sides* believe the other owns the filesystem.

It is stale host state, not the badge holding the drive. The cure is to make
the host re-read the bit by re-enumerating: reset the badge (`import
microcontroller; microcontroller.reset()` over the REPL is enough) and it
comes back `RO=0`. Distinguish it from the SD card, which is read-only over
USB permanently and by design - see the handoff.

**Also worth knowing:** the REPL mangles pasted `for` loops sent line by line.
Use paste mode - Ctrl-E, the lines, Ctrl-D - or wrap the whole thing in
`exec("...\n...")`. And do not wait for a sentinel that also appears in the
echoed source, which is a good way to think a 20-second test hung.

---

## 8. The budget is what is left over, not what is asked for

Setting `RAM_BUDGET` to 48 KB broke the sample browser, and it took a badge to
show it. The chain:

1. `StartupState` now prints one line at handover, because this is the number
   every memory decision here is made against and there was no way to see it:

   ```
   free after warm: 17120, kit 49152, samples 0
   ```

2. 17 KB is not enough to open a sample list. 98 paths, then 99 menu rows at
   165 bytes each, is roughly 25 KB at peak.
3. `os.listdir` raised `MemoryError`, and `list_samples` **caught it per
   directory and carried on** - turning "I could not read the listing" into
   "there are no samples".
4. `Catalog.samples()` cached that empty answer, and sample listings are
   deliberately never dropped. So the browser showed nothing but `(none)`, on
   every track, for the rest of the session.

Four fixes, three of which are worth having whatever the budget is:

- **`list_samples` no longer catches `MemoryError`.** An `OSError` on a
  directory is "no card, try the next store". A `MemoryError` is "I cannot
  answer", and answering "none" instead is a lie the caller cannot detect.
- **`Catalog.samples()` does not cache an empty result.** Flash always holds
  the shipped kit, so empty means the read failed rather than that there is
  nothing to play.
- **The screen says "Out of memory"** when `Menu.last_error` is set, instead
  of showing what looks like an empty folder.
- **`RAM_BUDGET` is 32 KB.** The kit gets what is left after the rest of the
  badge has what it needs, not the other way round.

Measured after, on the badge, through a full boot:

```
free after warm: 21184, kit 32768, samples 98
Track 5 -> rows=99 err=None free=9568
first row "(none)", second "1._175bpm_Break_one_1.wav"
```

Free rose only 4 KB for a 16 KB cut, which is the point: the other 12 KB is
the catalog now succeeding and holding 98 paths, where before it held nothing.

### What it costs

Shared across the four-track kit, 32 KB is **0.256 s a track**:

| track | loaded | seconds | trimmed |
|---|---|---|---|
| kick | 8,192 | 0.256 | yes |
| snare | 7,940 | 0.248 | no |
| open hat | 8,318 | 0.260 | yes |
| cymbal | 8,318 | 0.260 | yes |

That is short, and it is the honest ceiling for holding everything in RAM on
this board. **Option B - streaming from internal flash - is now the change
worth making**, because it buys length back without spending the memory the
browser and the settings tree need. The reason it was ranked second no longer
holds: the RAM path has been pushed as far as it goes.

---

## 9. Two more the budget caused, found by playing the badge

**"T5 failed", then everything sounds mangled.**

*Mangled audio.* Nothing dropped a menu's rows. A 98-sample list is 99 Items,
about 16 KB, and `Menu.back()` only popped the path - so after one visit to
the browser, free memory sat at **6,256 bytes** for the rest of the session.
That is under `main.py`'s `GC_FLOOR`, so the loop called `gc.collect()` on
every pass: 25 ms at a time against a 32 ms audio buffer. Every sample sounds
mangled, long after the menu was closed, and nothing about it points at the
menu.

Fixed by letting go on the way out - `Menu.back()` invalidates the branch it
leaves, and `SettingsState.exit()` drops every card-backed list. Measured on
the badge: 6,256 free inside the list, **22,592 after backing out**.

*"T5 failed".* A kit spends the whole budget between the tracks that have
samples, so a track that had none has nothing left to spend and
`_allowance()` returned 0. `sequencer.assign_sample` now reloads the kit so
the budget is divided by the tracks that want it now, rather than by the ones
that wanted it before.

Two things had to change for that to work:

- **`load_kit` releases every track before loading any.** Releasing each as
  its turn came left the budget held by the tracks behind it, so the share
  was a fraction of a fraction and the *first* track could not fit its own
  sample.
- **`_set_sample` leaves the listing first**, as `_delete` already did. With
  the 16 KB of rows still held there was not enough heap to reload the kit,
  and the reshare failed silently into "no room".

Measured on the badge, assigning a card sample to track 5 with a full
four-track kit already loaded:

```
MSG    T5 1._175bpm_Break_one_1.wav
ALL    [True, True, True, True, True, False, False, False]
SIZES  [6552, 6554, 6554, 6554, 3276]
FREE   20224
```

Track 5 got half a share rather than a whole one - the first allocation of its
full share failed and `_read_audio` halved, which is the fragmentation
backstop working rather than a fault.

### The shape of the ceiling

Every one of these is the same problem wearing a different hat: **the kit, the
sample browser and the settings tree are all competing for about 54 KB, and
holding samples in RAM is what makes the kit's share so large.** Eight tracks
share 32 KB, which is 0.128 s each.

That is the argument for **option B**. Streaming from internal flash takes the
kit out of the competition entirely - the RAM path has now been tuned as far
as it goes, and every remaining problem here is a consequence of its ceiling.

---

## 10. Coming back as the badge was left

The kit is now a kick, a snare, a closed hat and an open hat. A crash says
nothing at all in the quarter of a second the budget allows; a hat pair says
most of what a beat needs and both halves are already that short. Trimmed to
0.25 s, the four spend 31,934 of 32,768 bytes and every pad sounds.

`sequencer.restore()` replaces the unconditional demo at import. In order, each
allowed to fail on its own: the song last saved or loaded, the samples last
assigned over the top of it, and the shipped kit and demo pattern if there was
no song to find. **Nothing in it may raise** - it runs at import, so an escape
is a badge that will not start, and everything it reads is on a card that can
be removed or written by another machine.

`prefs` now remembers, alongside brightness, screen text and animation:

| key | written when |
|---|---|
| `song` | a song is saved or loaded |
| `kit` | a sample is assigned or cleared, or a kit or song is loaded or saved |
| `volume` | the knob has been still for 1.5 s **and** nothing is sounding |

The volume rule has two conditions and both matter. Still, because a hand
crossing the range produces a detent per pass and each would be a file write.
Quiet, because a card write is tens of milliseconds against a 32 ms buffer,
and a tick under a drum hit is a poor price for remembering something nobody
is waiting on. A badge switched off mid-pattern without ever stopping forgets
the change; that is the trade against hearing every one of them.

`-1` means "never saved", because 0 is a real position meaning silence and
cannot double as unset.

Verified on the badge across a hard reset: samples swapped to
`hh_hats-closed_3` and `hh_hats-open_2` came back, and a volume turned from 37
down to 31 came back at 31.

**Unsaved pattern edits do not survive.** Steps are held in memory until a song
is saved, and autosaving them would mean a card write per edit. Explicit save
is the usual bargain for a sampler, but it is a bargain, not an oversight.

## 11. Trailing silence

`Tools/convert_samples.py` removes the run of near-silence at the end of a
sample by default (`--no-trim-silence` to keep it, `--silence-db` to move the
threshold from -60 dB). Scanned backwards from the end, so a quiet passage in
the middle of a sound is never mistaken for the end of it.

Measured across the 98-sample library on the card:

```
2,397,997 frames -> 1,964,217   18.1% was trailing silence, in 89 of 98 files
  3_defcon.wav                9.751 s of air
  4_welcome.wav               7.440 s
  2_welcome.wav               2.807 s
  1_hackers.wav               1.552 s
  4._175bpm_Break_four_7.wav  0.232 s
```

Worth knowing where this does and does not buy anything. A sample longer than
its share is already only read as far as the share goes, so air past that point
costs nothing. It is the samples **shorter** than their share that pay - the
whole file is loaded, silence and all, out of a budget every other track is
sharing. Trimming the same run at load time would recover that on the badge
rather than only in the tool, and is the obvious next thing if the budget stays
this tight.

---

## 12. The held knobs say what they just did

While a modifier is held the legend has the screen, and it says what each knob
is *for* rather than what it just *did* - so turning one changed a setting with
nothing to show for it. `_message` existed but `_render_display` returned early
on the legend, so it could never be seen.

The line for the knob being turned now carries its value, and the other two
keep saying what they are for:

```
Function + Vol  ->  ['pad  track', 'Sel  pitch',          'Vol  T1 95%']
Function + Sel  ->  ['pad  track', 'Sel  pitch: not yet', 'Vol  volume']
Play     + Sel  ->  ['pad  page',  'Sel  len 10',         'Vol  quantize']
Play     + Vol  ->  ['pad  page',  'Sel  length',         'Vol  quant 100%']
```

Two details worth keeping:

- **The line is found by its label, not its position.** The pads' legend has
  Sel and Vol on the first two rows where Function's has them on the last two,
  so a fixed index writes the value over the wrong row.
- **Nothing is said when nothing is held.** The detail line already shows the
  tempo, the division, the length and the volume, all of them, all the time -
  a message there hides more than it adds. An intermediate version of this
  change reported unconditionally and put "121 BPM" over that line for a
  second every time the tempo knob moved; two tests now hold that shut.

---

## 13. A modifier's pad press was spent twice

Choosing a track with Function+pad, or a page with Play+pad, also toggled that
pad's step. `Controls.press` consumed the press - it returned `SELECT_TRACK`
or `PAGE` rather than `PAD` - but `Controls.release` returns `PAD_RELEASE`
unconditionally, and `SamplerState` toggles on release unless the pad is in
`_edited`. Only the velocity-edit path ever put anything there.

So the gesture did two things: the one it was for, and one nobody asked for.
Worse than noise, because it is silent - the note appears on a track you have
just navigated away from looking at.

Fixed where the mechanism already lived: `SELECT_TRACK` and `PAGE` mark the
pad as spent, exactly as a velocity edit does. `ERASE` does not need it - it is
a LIVE gesture, and release only toggles in SEQ.

Measured on the badge:

```
BEFORE      track=0 page=0 step33=False
AFTER_FN    track=3         step33=False   selected the track, wrote nothing
AFTER_PLAY  page=1          step9=False    changed the page, wrote nothing
PLAIN_TAP   step=True                      a plain tap still toggles
```

The last line is the one worth keeping a test on: suppression that outlives
the press it was for is the same bug facing the other way, and the four tests
here fail against the old code in all four directions.

---

## 14. The sampler had no room to play in

Two reports: saving crashed the badge, and a 16-step pattern played its samples
only sometimes. One cause behind both.

`StartupState` prints free memory at handover, and it read:

```
free after warm: 17360, kit 32768
```

`main.py` collects whenever free drops below `GC_FLOOR`, which is 16,384. The
badge was running **976 bytes above the collection floor**. The loop allocates
a couple of bytes a pass at a few thousand passes a second, so it crossed that
line every few hundred passes and collected - 25 ms at a time, into a 32 ms
audio buffer. That is a pattern whose samples play only sometimes.

Where the memory had gone: `SettingsState.Catalog` held the sample listing for
the whole session, warmed at boot so the browser would open quickly. Measured
by dropping it on the badge, 98 names and 98 paths is **about 12 KB** - and it
was held whether or not anybody ever opened the menu.

That was a reasonable trade when the kit was small. It is not one now.

- **The samples are no longer warmed at boot.** Songs and kits still are: a
  handful of short names is nothing like the same cost.
- **The listing is dropped when the settings screen closes**, alongside the
  rows, so the sampler gets it back.
- **The boot diagnostic no longer reports the sample count.** Measuring it
  meant asking for the listing, which read the card and then held the 12 KB -
  reintroducing the exact problem, from the line that was supposed to reveal
  it. A diagnostic that costs more than what it reports is not worth having.

The cost is a stall on the first sample list opened, where the player has just
asked for something. It used to land on the thing they were listening to.

Measured after, on the badge:

```
free after warm: 30768, kit 32768        (was 17360)
PLAYING passes=3000 collects=9 secs=20.32 errs=0
save song -> "saved MYBEAT",  save kit -> "saved MYKIT",  free 29024 after both
```

Nine collections in three thousand passes with a sixteen-step pattern running,
and no audio errors.

### On the crash

It did not reproduce, before or after. What was found and closed is a real one
introduced with the persistence work: `_remember_song` and `_remember_kit` are
best-effort notes to a card that may be absent, full or slow, and they ran
inside the action handler with nothing catching them. Building the list of kit
paths allocates, at the moment the heap is most spoken for, so a `MemoryError`
there went straight out to the main loop. Both now swallow, and say why.

That plus 13 KB of headroom is the most likely account of it, but it is an
account, not a reproduction. Worth trying again.

---

## 15. Review findings not acted on

From the pre-commit review (security, quality, CircuitPython). Acted on: the
unguarded `prefs` reads at import that could brick the badge on a malformed
`settings.prefs`; a stale `Menu.last_error` reporting "Out of memory" on the
next unrelated action; a stale `Sequencer.last_error` misreporting why a sample
would not load; `_rename` leaving `prefs.last_song` pointing at a file it had
just moved; `last_kit` unable to tell "never saved" from "every track
deliberately silenced"; and a tuple allocated per pass in `_render_pads` by the
comparison written to avoid allocating.

Left alone, deliberately:

- **`prefs` accessor naming is inconsistent** - `animation_name()` /
  `set_animation()`, `last_song()` / `set_last_song()`, `volume_position()` /
  `set_volume_position()` are three conventions in one file. Fixing it touches
  every caller across four modules and the tests; it is a rename, not a bug,
  and worth doing deliberately rather than inside a change about something
  else.
- **`Menu._forget_other_branches` may be redundant with `back()`** now that
  `back()` invalidates the branch it leaves. The full-tree sweep on every
  `enter()` covers paths that do not go through `back()` - `refresh()` and
  `reset()` - but whether either is reachable with a built sibling was not
  established. Worth proving before removing.
- **`_nudge_velocity` reports one held step's level when several are held**,
  where `_track_volume_text` reports a count. A wording decision about a
  legend line, not a correctness one.
- **`_read_audio`'s floor is `MIN_RAM_SAMPLE` for tracks and `FRAME_BYTES` for
  auditions.** The asymmetry is intentional - a preview may legitimately be
  tiny - but nothing says so.

Not yet verified on hardware: `prefs.py` was rebuilt from `HEAD` plus this
session's edits after being truncated to zero bytes by a bad edit script, and
the badge was disconnected before it could be deployed. The gates and 1223
tests pass, including new ones covering the rebuild, but it has not been run.

---

## 16. Noted, not acted on: the keypad scans every 20 ms

Found while planning the rewrite, verified against the CircuitPython 10.2.1 source, and
deliberately left alone - recorded here so it is not rediscovered.

`setup.py:110` constructs `keypad.KeyMatrix` with `row_pins`, `column_pins` and
`columns_to_anodes`, and no `interval`. The default is `0.020f`
(`shared-bindings/keypad/KeyMatrix.c:104`), and `interval_ticks = interval * 1024`
(`shared-module/keypad/__init__.c:95`). So the pads are scanned every 20 ms.

Debounce adds nothing on top: `debounce_threshold` defaults to 1, and the counter in
`shared-module/keypad/__init__.c:125` reports a transition on the *first* scan that sees
the new state.

**A pad press therefore waits at least 0-20 ms merely to be noticed** - before the
sequencer, the mixer or the DMA do anything. That is plausibly comparable to the 32 ms
mixer buffer everyone has been looking at, it has been there the whole time, and nobody
chose it.

**20 ms is a floor, not the worst case.** `keypad_tick()` schedules the scan as a
background callback rather than performing it in the tick interrupt - and section 1 of
this document establishes that background callbacks are deferred inside SPI transfers
and blocked outright while `background_prevention_count` is held. A collection is
25-27 ms on top. So the tail is 20 ms plus whatever the worst deferral is, and nobody
has measured that.

One more caveat on provenance: the badge runs `Firmware/DCZiaSampler.uf2`, supplied by
DCZia, and nothing here records whether it is stock 10.2.1 or a custom build. Reading
the upstream source proves what upstream does, not what is on this chip. The constant
should be read back at runtime before anyone relies on it.

`keypad.KeyMatrix(..., interval=0.001)` is the one-line change. Untested: it costs a
faster scan of a 3x4 matrix on every pass, which is not free, and the interaction with
`debounce_threshold` at that interval has not been measured.

Not pursued now - the decision is to focus on the rewrite. Worth taking before anyone
concludes from a latency measurement that the runtime is the reason.
