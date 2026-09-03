# Phase 2: sequenced step timing - 2026-09-02

The claim this phase rests on is that deriving the clock from audio frames rather than
from a millisecond timer makes step timing sample-accurate. This is the check.

## Method

One track, one step, length one, so every hit is the same event and the spread is not
confounded by which step fired. A 64-frame tone, short against the step so the pin is
quiet between hits.

For each hit: the frame the sequencer scheduled it for, against the moment the data pin
first went high, timed by a second PIO state machine. Both measured from one fixed
reference taken at `seq_start` - not from `audio_frames()`, which only advances at block
boundaries and would contribute up to 2 ms of staleness indistinguishable from the thing
being measured.

The difference carries a constant: the ping-pong buffering, the TX FIFO, the PIO
serialisation, and the bit position of the first set bit. That constant is latency, not
jitter, and it is not what is under test. **The spread is.**

## Three bugs this found

Worth recording in order, because two of them were in the firmware and one was in the
test, and all three produced a plausible number rather than an error.

**1. A sub-block offset computed on the wrong core - 5,745 us of spread.**

Voices originally took "start this many frames into the next block", computed on core 0
against `audio_frames()`. That counter only advances at block boundaries, so the block
core 0 measured from and the block the mixer filled next need not be the same one.

Voices now carry an **absolute target frame** and the mixer places them. It is the only
thing that knows which block it is about to fill.

**2. The tick boundary overflowed after two days.**

`start_frame + per_tick * n` in 32.32 fixed point. `per_tick` is about 1.4e12 and `n`
reaches 1e7 after roughly 58 hours of continuous play - inside the time somebody leaves a
sampler running. It now carries a whole part and a fraction, which is exact and cannot
overflow in any lifetime.

Rounding `per_tick` to whole frames would have been the other tempting fix, and worse: at
120 BPM a tick is 333.33 frames, so rounding loses a frame every three ticks - two and a
half seconds of drift an hour.

**3. The test measured the previous hit - 6,483 us of invented spread.**

The watcher re-arms the instant the data pin goes high. Clearing its interrupt *after*
recording a hit left the tail of that same tone to re-raise it immediately, so the next
iteration returned at once on an edge belonging to the hit before. Clearing before the
hit sounds - the pin is quiet then, because scheduling happens at most one block ahead -
fixed it.

This one is worth as much as the other two. A measurement that reports 6.5 ms of jitter
that does not exist would have sent someone looking for a fault in the sequencer.

## Result

**measured**, 120 hits per trial at the pin, first hit of each trial discarded and reported
separately:

| BPM | division | frames/step | **spread** | min_at | warm-up | late | underruns |
|---|---|---|---|---|---|---|---|
| 240 | 1/16 | 1000.000 | **0 us** | 0 | 19,863 | 0 | 0 |
| 127 | 1/16 | **1889.763** | **1 us** | 4 | 34 | 0 | 0 |
| 143 | 1/8T | **2237.762** | **1 us** | 4 | 45 | 0 | 0 |
| 97 | 1/32 | **1237.113** | **1 us** | 1 | 33 | 0 | 0 |
| 300 | 1/4 | 3200.000 | **0 us** | 0 | 30 | 0 | 0 |

**Nought to one microsecond of spread**, where one microsecond is the resolution of the
timer doing the measuring. The jitter is smaller than the instrument can see, and far
below the 62.5 us a single frame is worth.

Against the current firmware, whose clock is the main loop and whose loop Phase 0
measured at 8.5 ms typical and 46 ms worst, that is about four orders of magnitude.

Three of the five trials have a step length that is deliberately not a whole number of
frames - 1,889.763 and 2,237.762 and 1,237.113 - so the fractional carry in the tick
accumulator is exercised rather than sitting idle. A version that rounded to whole frames
would drift two and a half seconds an hour and would have passed the 240 BPM trial
perfectly.

The per-trial constant is latency, not error: the transport now starts a full lookahead
ahead, so every hit including the first sounds one pipeline after the frame it names.
16 ms of that is the lookahead and about 4 ms is the ping-pong buffering plus the FIFO.

The `warm-up` column is the discarded first hit, and it is what identifies the remaining
artefact as the measurement's rather than the firmware's: the first trial has no previous
tone and reads a normal 19,863, while every later trial reads 30-45 - a stale edge left by
the previous trial's last tone, which a hit booked 16 ms in the future cannot possibly
have produced.

**Not covered:** 1/8 and 1/16T were not run. Both sit between divisions that were, and the
tick counts differ only by which whole number divides 24, so the risk of a surprise there
is low - but it is untested rather than tested.

## What the host tests cover instead

`Rewrite/tests/test_seq.c` runs on the build machine, where the pure arithmetic can be
enumerated rather than sampled: every step firing exactly once per cycle, a three-step
track against a four-step one coinciding once in twelve, offsets moving hits the right
direction, mutes, velocity, and offsets being re-clamped when a division shortens.

That split is deliberate. The badge measures latency, jitter and underruns because only
the badge can. A laptop checks tick attribution exhaustively in a millisecond, and it is
where the one-step double-fire was found.

## Reading the Python's own song files

Phase 2 reads the existing `.song` format rather than inventing one, so the player's work
survives the rewrite. Proving that needed a file the Python wrote, and `/songs` on the
card was empty - so CircuitPython was restored, told to save a song with deliberately
awkward values, and the C firmware then read it back.

**measured**, field by field:

| field | CircuitPython wrote | C read |
|---|---|---|
| bpm | 137 | 137 |
| division | 1 (1/8) | 1/8 |
| lengths | 5, 3, rest default | 5, 3, 8 |
| muted[2] | True | 1 |
| track_volume[0] | 1.5 | 6144 in Q12 = 1.5 |
| step (0,0) | velocity 111, offset 0 | 111, 0 |
| step (0,2) | velocity 64, offset +1 | 64, +1 |
| step (1,1) | velocity 90, offset -1 | 90, -1 |

Every field exact.

### What the file actually looks like, and why guessing failed

The first attempt returned `not_a_song` after correctly reading `bpm`. Two things about
the real bytes were wrong in the assumptions:

**The keys are not in the order `to_dict` writes them.** CircuitPython dictionaries
iterate in hash order, so the file begins `bpm`, `kit`, `track_strength`, `v`. A reader
that matched keys positionally would have worked on one build and broken on the next.
This one matches by name, so the order does not matter.

**The Python writes `None` for a per-track value it has never set.** `track_volume` came
out as `[1.5, None, None, None, None, None, None, None]` - one float and seven nils. A
reader expecting eight numbers consumed 8 bytes where it should have consumed 41, and
every key after that point was read out of the middle of a value.

That is this format's characteristic failure: **one misread value and the rest of the
file is garbage**, with no checksum to notice. The fix is a nil case wherever a per-track
value can be absent - volumes, mutes and step rows alike.

Worth stating plainly: this was only found by reading a file the Python actually wrote.
A reader tested against files it had written itself would have passed, because it would
never have produced a nil.
