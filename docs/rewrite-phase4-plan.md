# Rewrite, Phase 4: the rest of the instrument

Phases 1-3 made it play, sequence and save. What they did not do is give the player
the controls the Python already has. Eight pads, two knobs and two buttons were
carrying five gestures out of seventeen; the other twelve were reachable only by
editing a file on the card.

This phase is about **parity with a firmware the user already plays**, which is a
different job from designing controls. `engine/controls.py` is the specification and
it is followed rather than improved on, because the player has muscle memory and a
better idea that has to be learned is worse than a familiar one that works.

## The gestures, and what they cost to add

`controls.py` lists seventeen. Five were already in place. The twelve added here:

| Gesture | Meaning | Notes |
|---|---|---|
| Function + Play | arm or disarm recording | new: live capture, below |
| Function + Volume click | clear the selected track | |
| Function + Select turn | selected track's pitch | semitone a click |
| Function + Volume turn | selected track's volume | |
| Play + Select turn | pattern length | per track, so polyrhythm stays reachable |
| Play + Volume turn | quantise strength | |
| Play + pad (SEQ) | jump to that page | the only way to reach step 64 on eight pads |
| Play + pad (LIVE) | erase that track as the playhead passes | |
| pad + Select turn | that track's pitch | see below |
| pad + Volume turn | step velocity in SEQ, track volume in LIVE | |
| Volume click | mute the selected track | |
| Play tap | *moved to release* | see below |

Two entries need explaining rather than listing.

**`pad + Select turn` is documented as "pitch: of that step in SEQ, that track in
LIVE".** Per-step pitch does not exist in the song model - not here and not in the
Python, whose own sequencer notes it as not yet done. So both modes move the
track's pitch, which is the half that is implementable, and the code says so
instead of implying the other half works.

**Play now acts on release.** It is both the transport button and a modifier for
four other gestures, so toggling on press meant that holding Play to erase, or to
change pages, also started the song. `controls.py` names this trade explicitly: the
transport waits the length of a tap. Function already worked this way; Play now
matches it, with a `play_used` flag set by anything pressed or turned against it.
The subtle case is the menu - entering an item can close the menu, so the release
would find no menu open and start the song. Marking Play used on entry, rather than
guarding the release on whether a menu happens to be open, closes that.

## Live recording

Arming turns LIVE pads into a recorder: the pad sounds, and the hit is written to
the nearest step of that track.

The interesting part is what "now" means. The sequencer books hits up to eight
blocks ahead so each one can be handed to the mixer on an exact frame, which means
`seq->tick` is deliberately in the future. A pad struck now belongs where the
player *heard* the beat. So `seq_now` reads the audio frame counter and works
backwards from the downbeat instead.

Two details in that conversion were worth the test that found them:

- It **rounds** to the nearest tick rather than truncating. A hit landing a hair
  inside a boundary is the player being fractionally early for that beat, and
  truncation files it one tick early, every time.
- It multiplies by `bpm * PPQN` and divides by `rate * 60`, rather than dividing by
  the 32.32 frames-per-tick figure the scheduler uses. That figure needs the frame
  count shifted up by 32 first, which overflows after 2^32 frames - about 27 hours
  of running transport - and then records into the wrong bar rather than failing.

## Quantise belongs on playback

The first version applied strength when the hit was captured. That is destructive:
turn the knob back afterwards and the feel is gone, because it was never stored.

`engine/quantize.py` applies it on the way out, and it is right. The offset in the
song is what the player actually did; the knob decides how much of it survives to
the scheduler. `seq_effective_offset` reproduces its arithmetic in twentieths -
`STRENGTH_STEP` is 0.05, so twenty positions is exactly the resolution the knob
already had, with no float in the scheduling path.

This also settles a question the earlier version got wrong. There is **no** gesture
in either firmware that edits an offset by hand - `pad + Volume turn` in SEQ is
velocity. That is what makes a default strength of 100% coherent: offsets are a
record of how a pattern was played, not something dialled in. An offset the player
set by hand would be snapped away by the default setting, which is a trap. The
gesture was mismapped here and is now velocity, as documented.

Tracks can carry their own strength, overriding the global knob, which is how one
track swings while the rest stay straight. It is read, written, and honoured.

## Two settings that were being silently destroyed

The Python's loader reads every key with `data.get(key, default)`. A key the C
writer omits therefore does not fail to load - it **resets**.

The writer was emitting `track_strength` as all-nil and omitting `kit_volume`
entirely. Any per-track quantise override or kit trim the player had set would
disappear the first time they saved from the badge, looking exactly like nothing
had happened. Both are now carried through: read, held on the song, and written
back, whether or not this firmware acts on them.

## What made that findable

The format's encode and decode were welded to the filesystem, so the only way to
exercise them was to write a card and read it back on hardware. Every bug this
format has had was found that way, at the cost of a power pull each: a float
encoder that doubled its input, a reader thrown out of step by an unexpected nil,
keys arriving in the Python's hash order, and now two dropped fields.

They are all arithmetic. `songfile_encode` and `songfile_decode` are now separate
from `songfile_save` and `songfile_load`, and `tests/test_songfile.c` round-trips a
song with something distinctive in every field. It was checked against the defect
it was written for by reintroducing it - both dropped fields fail the test - which
is the only way to know a test is doing anything.

## Display

Two states needed showing and there were four spare pixel rows to show them in.

- **Muted** strikes a bar through the track's cell. Not dimmed, because the panel
  has one bit per pixel and no dim to give; not blank, because blank is what an
  empty track already looks like and the two mean different things.
- **Armed** blinks a mark in the band between the pitch and level bars. No label
  beside it: that band is four pixels tall and the type is nine, so a word there
  would run straight through the level bar.

## Naming a saved song

`MENU_NAME` was reserved on the assumption that twelve keys means a name has to
be chosen from a list rather than typed. That turned out to be wrong in a useful
way: there are **two** knobs, and two knobs are two axes. Select moves along the
name, Volume changes the character under the cursor, and Play and Function keep
meaning save and back exactly as they do on every other screen.

The character set is space, A-Z, 0-9, `-` and `_`. No lower case, because FAT
upper-cases a short name anyway and offering a distinction the card does not
keep produces two names that look different and are the same file. The set
wraps, unlike a list - there is no "how far through am I" to lose, and stopping
at Z would mean turning back thirty-eight clicks to reach a space.

The cursor is a rule under the character rather than an inverted block. Every
other screen uses an inverted block for the selected item, and two different
things should not look the same.

Two things the tests caught that a knob would have found slowly:

- `menu_open` memsets the whole struct, so seeding the name **before** opening
  silently did nothing - which is exactly what the first wiring did. The name is
  now explicitly blanked on open and seeded after it.
- An empty name is refused rather than saved. It would write a file the song
  list cannot show, leaving the player no way to load their own work back.

## Still missing

MIDI, HID, animation, and the sync jacks. Per-step pitch, which neither firmware
has.
