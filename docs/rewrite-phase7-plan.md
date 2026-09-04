# Rewrite, Phase 7: MIDI

The 5-pin jack on GP8/GP9 at 31250 baud. USB MIDI is the second half and is not
done yet - see the end.

## The parser is the part that matters

A MIDI stream is not a sequence of self-contained messages, and the three ways
it is not are the three ways a parser passes a test program and then fails
against an instrument.

**Running status.** A data byte arriving where a status byte was expected reuses
the previous status. A keyboard playing a fast trill sends the status once and
then pairs of data bytes for as long as it likes.

**Real-time bytes interleave.** 0xF8–0xFF may appear between *any* two bytes,
including the two data bytes of a note, and must disturb neither the running
status nor the half-assembled message they landed inside. Clock is 0xF8 and
arrives 24 times a quarter note, so this is not an edge case: at any real tempo
a parser that lets 0xF8 clear its state drops every note played while a master
is running.

**Note on at velocity zero is note off.** Almost everything sends it that way,
precisely so running status can be held across both. A parser that only knows
0x80 hangs every note such a keyboard plays.

There is a fourth that is quieter: program change and channel pressure carry
*one* data byte. Reading two swallows the next status byte and desynchronises
everything after it.

All four have a test. The parser is in `src/midi.c` with no hardware in it - the
ports are in `src/midi_port.c` - so those tests run on the host, the same split
the song format got and for the same reason.

## Clock out is scheduled, not sent

MIDI clock is 24 a quarter note, which is exactly this engine's tick rate, so
every tick is a clock byte.

Sent when the tick is *processed*, every one of them would leave sixteen
milliseconds early - the sequencer's lookahead, which is what lets it place each
hit on an exact frame. A constant offset is less harmful than jitter, since the
receiver simply runs early, but it is still wrong and the machinery from the
sync jack is already here. So clock bytes go on a hardware alarm scheduled
against `audio_frame_time_us`, through an eight-deep queue: at 300 BPM a tick is
8.3 ms and the lookahead is 16, so two or three are booked before the first goes
out.

Measured on the badge at 142 BPM: **1648 clock bytes for 1648 ticks, none
dropped.**

## Notes are lifted

`sequencer.py` sends `NoteOn` and never `NoteOff`. That is defensible when the
thing on the other end is a drum machine playing one-shots, and wrong when it is
a synth: those notes hang, and the way out is a panic message or a power cycle.

Here a note is lifted 20 ms after it is struck. Retriggering restarts the hold
rather than lifting first - a drum part hitting the same pad twice quickly
should read as two hits, and an off between them would make the second a
retrigger of something already stopping.

Measured: 2049 bytes for 342 hits, which is 342 note-ons and 341 note-offs with
one still held.

## Two clocks, one transport

The external clock built for the sync jack takes the rate as an argument now,
because the jack's rate and MIDI's are not the same thing: MIDI clock is fixed
at 24 by the standard while the analog input is whatever the player selected.
Both drive the same transport without either knowing about the other, and a
change of rate throws away a history measured against the old one.

Two loops are refused explicitly. The badge sends no clock while following
someone else's, and sends no start or stop while slaved - a device echoing its
master back at itself is how a feedback loop starts.

## Writes never block

A MIDI cable with nothing on the other end still clocks out at 31250 baud, but a
receiver holding the line must not be able to stall the sequencer. Every send
checks writability and gives up rather than waiting. Dropping a clock byte is a
glitch; blocking the main loop is an underrun.

## Measured, with the rest running

At 142 BPM, with the light strip animating and the sync jack pulsing:

```
underruns=0  worst_cycles=8811  peak=7 voices
syncout=138  syncerr=16  midiclk=1648  mididrop=0
```

8,811 cycles of the 250,000 a block has.

## Not done: USB MIDI

The firmware is CDC only. Adding USB MIDI means a composite descriptor, which
puts the CDC console - the path this session uses to flash the badge and read
every measurement above - at risk if it goes wrong. It is worth doing and worth
doing as its own step.

## Not verified

Nothing has been plugged into the jack. The byte counts prove the right bytes
are being generated in the right numbers at the right times; whether a synth on
the other end plays them is something only a cable can answer.
