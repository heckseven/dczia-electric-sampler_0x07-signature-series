# Rewrite, Phase 6: the light strip

Ten WS2812s on GP3, and two things worth having on them.

## Why the animations are functions of the tick

`engine/animation.py` opens with the reason it exists:

> *"The library animations this replaces could not see the music. They ran on a
> wall clock, so a chase at 0.1 s a step drifted against a 120 BPM pattern and
> matched nothing; at any other tempo it was simply unrelated."*

Everything here is a function of the sequencer's tick instead. That is
reproduced rather than redesigned, and it buys something for free: the transport
already latches to an external clock and flywheels through gaps in it, so these
follow a drum machine plugged into the sync input without knowing that happened.

Nine animations: pulse, chase, comet, sweep, rainbow, sparkle, heartbeat,
toaster, and off. Rewritten in integers - phases are 0-256 rather than 0.0-1.0,
because this chip emulates floating point in software and there is no reason for
the strip to spend cycles the mixer might want.

They keep moving with the transport stopped. `seq_display_tick` returns the
transport's tick while it runs and a free-running one at the current tempo
otherwise, so the strip is already at the tempo when Play is pressed rather than
jumping to catch up.

## Driving the strip

A WS2812 wants 24 bits a pixel at 800 kHz with sub-microsecond bit timing, which
is exactly what a bit-banged driver cannot promise on a chip that is also mixing
audio. The CircuitPython library disables interrupts for the length of the
transfer - about 300 µs for ten pixels - every time it shows a frame.

Here the PIO holds the timing and a DMA channel feeds it. Pushing a frame is one
register write. It runs on **pio1**, because pio0 is the I2S output: four state
machines each, and putting the strip on the other block means a future program
on either cannot crowd out the one that must never stall.

Two frames are skipped rather than sent: one identical to what is already
showing, which at a slow tempo is most of them, and one arriving while the last
is still going out. Dropping the second is right - the next pass will send
whatever is current, which is fresher than what would have been queued, and
nobody can see one frame of a light strip.

## The geometry is measured, not derived

`utils.neoindex` records what was actually underneath each pixel when they were
lit one at a time:

```
pixel 0    Function
pixel 1    Play
pixel 2-5  upper pad row, RIGHT to left  (pads 4, 3, 2, 1)
pixel 6-9  lower pad row, left to right  (pads 5, 6, 7, 8)
```

Its docstring records that two earlier tables derived from the board layout were
both wrong: the LEDs are on the back copper of the front panel and the switches
on the front copper of the main board, and nothing in any file records how the
two are mounted relative to each other. The table is copied verbatim.

So the strip snakes, and anything that travels has to be told the path rather
than walking the strip in index order. A lap is positioned by where it is *in*
the bar rather than counted in sixteenths - ten pixels do not divide sixteen
sixteenths, and counting would bring a chase home only every five bars.

## Two things to show, ten pixels

They take turns. The pads say what the instrument is doing whenever it is doing
something - playhead in SEQ, sounding and selected and muted in LIVE, transport
on the Play pixel, view or external clock on the Function pixel. The animation
has the strip the rest of the time. Showing both at once would mean neither read
clearly.

## Measured

Playing the saved song at 142 BPM with the strip running:

```
underruns=0  worst_cycles=8809  peak=7 voices  pixframes=667
```

8,809 cycles of the 250,000 a block has - unchanged from 8,893 measured before
the strip existed. The driver costs nothing the mixer can see.

## Not yet verified

That the colours are the right colours and land on the right pixels. The frame
counter proves frames are being sent and the audio proves they are free; whether
pad 3 lights when pad 3 is struck is something only a person looking at the
badge can answer.
