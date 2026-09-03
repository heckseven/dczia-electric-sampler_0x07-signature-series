# Rewrite, Phase 2: the sequencer, timed by the audio clock

Phase 1 proved the core: 16 voices at 6.5% of a core, 4.56 ms to the pin, zero underruns,
the player's own samples at full length. What it does not do is sequence, which is most of
what makes this a sampler rather than a set of pads.

## The decision this phase turns on

**The sequencer runs off the audio frame counter, not a millisecond timer.**

The current firmware drives its clock from `supervisor.ticks_ms()` and advances it from
the main loop. That makes step timing exactly as accurate as the loop is punctual, and
Phase 0 measured the loop at 8.5 ms typical with a 46 ms worst case - the tail being the
garbage collector. At the default 1/16 division a step is 125 ms, so a step can land more
than a third of a step late.

Deriving the clock from frames removes the question. The audio core produces exactly
16,000 frames a second because a crystal says so, and a step boundary computed in frames
is a specific sample, not a moment the loop might notice late. Two consequences:

- **Step timing becomes sample-accurate**, 62.5 us, from ±46 ms.
- **The sequencer cannot drift against the audio**, because it is counting the same
  thing the audio is.

This is the "deterministic step timing" from the original brief, and it is the one part of
that brief Phase 1 did not already deliver.

To use it, voices need to start partway through a block: a step that falls 11 frames into
a 32-frame block must sound 11 frames in, not at the block boundary. So `audio_trigger`
grows a frame offset, and the mixer skips that many frames for that voice on its first
block.

## The model, unchanged from the Python

Reproduced rather than redesigned, because the player already knows it and `engine/song.py`
is a considered design:

| | |
|---|---|
| tracks | 8 |
| steps | up to 64, 8 to a page |
| velocity | 0 = off, 1-127, default 100 |
| micro-timing | per step, in ticks, ±`(ticks_per_step - 1) / 2` |
| length | **per track**, so tracks of different lengths give polyrhythm |
| divisions | 1/4, 1/8, 1/8T, 1/16, 1/16T, 1/32 - 24, 12, 8, 6, 4, 3 ticks at 24 PPQN |
| tempo | 20-300 BPM |
| per-track | volume 0.0-2.0, mute, strength |

Every division divides 24 exactly, so none of them drifts against the others - that
property is worth keeping and worth stating.

## Scope

**In:** the song model, the clock, transport, sample-accurate triggering with velocity and
micro-timing, step editing from the pads, page navigation, loading existing `.song` files
from the card, and a display showing the grid and the playhead.

**Out, and deliberately:** saving, menus, MIDI, HID, animation, and the sync jacks.

**Saving is out because it needs FAT32 writes, and that is the first thing in Phase 3.**
Writing a filesystem means allocating clusters, updating two FAT copies and rewriting
directory entries, on the card holding the player's work. Phase 1's storage layer is
read-only on purpose. Doing writes properly - and proving they cannot corrupt a card
mid-write - is a phase's worth of care, not a footnote to this one.

The consequence is honest and should be said plainly: **in Phase 2 you can load and play
your songs and edit them live, and edits are lost at power-off.** That is a real
limitation, and it is better than a half-tested write path aimed at the only copy of
somebody's work.

## Reading the existing format

Songs on the card are msgpack maps written by `store.Store`, with `steps` and `offsets` as
eight byte strings each. Phase 2 reads that format rather than inventing one.

The reason is not nostalgia. A new format means either migrating the player's songs or
stranding them, and a reader for a small msgpack subset - maps, arrays, ints, strings,
bin - is about two hundred lines. `Song.from_dict` treats every field as untrusted and
clamps it; the C reader does the same, for the same reason: the card is not something the
badge controls.

## Success criteria

1. A song already on the card loads and plays, recognisably.
2. **Step timing accurate to one frame**, measured at the pin over many steps, against
   the ±46 ms the current firmware allows.
3. Per-track lengths produce the polyrhythm they should - a 3-step track against a
   4-step track realigns every 12.
4. All six divisions play at the right rate, checked against wall clock over a minute.
5. Zero underruns while sequencing 16 voices.
6. Editing a step while the sequencer runs does not disturb the audio.

## Order of work

1. **Song model.** Plain data, no behaviour that belongs elsewhere.
2. **Frame clock and transport.** The heart of the phase, and the part worth measuring.
3. **Sub-block triggering.** `audio_trigger_at`, and the mixer's first-block skip.
4. **msgpack reader** and song loading.
5. **Editing and display.**
6. **Measure criteria 2, 3 and 4** on hardware.
