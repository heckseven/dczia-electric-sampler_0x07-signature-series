# Rewrite, Phase 5: the sync jacks

Two pins, GP7 out and GP6 in, and the place where the rewrite's clock is finally
worth what it cost.

## What the Python is up against

`engine/clock.py` carries a substantial apparatus for measuring an external
tempo: a history of pulse timestamps, an averaging window whose length scales
with the sync rate, bounds on what gap can be a tempo at all, and a clamp on the
result. Its own comments say why:

> *"Timestamps arrive in whole milliseconds, so a single interval is only as
> accurate as that resolution allows: at 2 PPQN the gap is 250 ms at 120 BPM and
> 1 ms of error is 0.4%, but at 24 PPQN the gap is 20.8 ms and the same 1 ms is
> nearly 5%."*

And, measured against a real MIDI master over DIN:

> *"a two beat baseline reported 131 to 156 BPM for a steady 137.6"*

That is not a flaw in the arithmetic. It is `ticks_ms` resolution plus whatever
the main loop was doing when the pulse arrived, and no amount of averaging
recovers information that was never captured.

## What changes here

**Incoming edges are timestamped in their own interrupt**, off the 1 MHz
hardware timer, in a handler that lives in RAM so an XIP miss cannot put a
variable delay into the one number whose entire value is that it has none. The
same measurement is a thousand times finer and does not depend on where the main
loop happened to be.

Averaging is still done, over four gaps rather than up to eight. A master's own
jitter is real and is what is left once the measurement stops being the problem.

**Outgoing pulses are scheduled, not sent.** This is the part that is easy to
get wrong and silently: the sequencer books ticks up to sixteen milliseconds
ahead, because that is what lets it hand each hit to the mixer on an exact
frame. Setting the pin at the moment the tick is processed would put every sync
pulse sixteen milliseconds ahead of the beat it marks.

So a pulse is scheduled on a hardware alarm for the microsecond at which that
frame reaches the pin.

## Knowing when a frame reaches the pin

`audio_frames()` counts frames the mixer has *produced*, and the DAC is a block
or two behind it - that is what a ping-pong buffer is for. Anything that has to
line up with what is heard needs the output's own clock, and the sync jack is
the first such thing.

`serve()` now records, each time it re-arms a finished channel, the hardware
timestamp and the frame the other channel is beginning to play. That pair is a
mapping in both directions:

- `audio_frame_time_us(frame)` - when a frame will be heard, for scheduling out
- `audio_frame_at_time_us(us)` - which frame was playing at a captured instant,
  for placing an incoming edge on the same timeline the sequencer schedules on

It is read from the other core, so it is guarded by a generation counter rather
than a lock: the writer is an audio path that must not block, and the reader can
simply try again.

At 16 kHz a frame is exactly 62.5 µs, which is 125/2 - so both directions are
integer arithmetic with no rounding step.

## Measured, on the badge, with nothing plugged in

The claim "the pulse leaves when the beat is heard" should be checkable without
a second device, so the output side reports its own error: how far the worst
edge landed from where it was asked for.

Running the player's own saved song at 142 BPM for forty seconds:

```
syncout=257  syncerr=16  syncmiss=0
underruns=0  worst_cycles=8893  peak=7 voices
```

**16 µs worst case**, no pulse ever scheduled into the past, and the audio
untouched - 8,893 cycles of the 250,000 a block has, with seven voices running.

## One place this deliberately differs from the Python

A gap several times the established period is treated as a master that paused
and came back, not as a master that slowed down: the history is discarded, the
measured tempo is kept, and the phase is still pulled onto the pulse.

`engine/clock.py` has no such case. The long gap goes into the average and the
result is clamped to `MIN_BPM`, so a two second pause drops the badge to 20 BPM
and it audibly winds back up over the following pulses. That contradicts its own
docstring -

> *"If external pulses stop arriving the clock does not stall: it keeps running
> at the last tempo it measured, and re-synchronises when pulses return."*

\- and this is what that sentence describes. The check is keyed on the measured
period rather than on the history, because a glitch immediately before a pause
clears the history but not the tempo, and bad-cable-then-pause is exactly the
combination where it matters.

## Phase, and what may not move

An arriving pulse pulls the schedule onto it, but only the part that has not
been booked yet. Ticks already resolved have handed their hits to the mixer on
frames that cannot be recalled, so the re-anchor adjusts `next_frame` rather
than `start_frame`.

A correction larger than one whole pulse is refused. It means the measurement or
the master jumped, and following it would either fire a burst of catch-up ticks
or stall for one. The tempo is taken; the phase is left.

## A test hook, and why it is not a debug backdoor

Measuring sync output needs the badge doing something and does not need a person
doing it. `console_set_command_hook` sends anything typed that is not `B` to the
firmware, so a script can start the transport and read the numbers back.

`B` stays where it is. A hook that could swallow the way back into BOOTSEL is a
hook that can brick the badge.

## Not yet verified

The pin toggles and the timing is measured, but nothing has been plugged into
either jack. A patch cable from GP7 to GP6 would close the loop end to end -
the badge would sync to itself, and `syncin` should track `syncout` exactly.
