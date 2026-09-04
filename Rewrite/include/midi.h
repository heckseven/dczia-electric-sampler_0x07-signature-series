/* MIDI: the 5-pin jack on GP8/GP9, and USB.
 *
 * The parser is the part worth being careful about, because a MIDI stream is
 * not a sequence of self-contained messages:
 *
 * - **Running status.** A data byte arriving where a status byte was expected
 *   reuses the previous status. A keyboard sending a fast trill sends the
 *   status once and then pairs of data bytes for as long as it likes.
 * - **Real-time bytes interleave.** 0xF8 to 0xFF may appear between *any* two
 *   bytes, including the two data bytes of a note - and must not disturb the
 *   running status or the half-assembled message they landed inside. Clock is
 *   0xF8 and arrives 24 times a quarter note, so this is not a rare case: at
 *   any real tempo it happens constantly.
 * - **Note on at velocity zero is note off.** Almost everything sends it that
 *   way, precisely so running status can be kept across both.
 *
 * Getting any of those wrong produces a firmware that works against a test
 * program and fails against an instrument. So the parser is a small state
 * machine with a host test rather than something checked by plugging a
 * keyboard in.
 */

#ifndef MIDI_H
#define MIDI_H

#include <stdbool.h>
#include <stdint.h>

/* The channel the badge speaks on, as the Python's midi_serial_channel - 1.
 * One-based to a player, zero-based on the wire. */
#define MIDI_CHANNEL 0

/* Fixed by the standard at 24 a quarter note, which is exactly this engine's
 * tick rate - so one clock is one tick. Told to the transport explicitly rather
 * than left to the sync jack's setting, because the two can differ and only one
 * of them is a MIDI cable. */
#define MIDI_CLOCK_PPQN 24

/* General MIDI drum range, kick upward - `note = 36 + track` in sequencer.py. */
#define MIDI_NOTE_BASE 36

enum midi_kind {
    MIDI_NONE = 0,
    MIDI_NOTE_ON,
    MIDI_NOTE_OFF,
    MIDI_CC,
    MIDI_CLOCK,
    MIDI_START,
    MIDI_CONTINUE,
    MIDI_STOP,
};

struct midi_message {
    enum midi_kind kind;
    uint8_t channel;
    uint8_t data1; /* note, or controller */
    uint8_t data2; /* velocity, or value */
};

struct midi_parser {
    uint8_t status; /* running status, 0 when there is none */
    uint8_t data[2];
    uint8_t have;
};

/* Feed one byte. True when `out` holds a completed message. */
bool midi_parse(struct midi_parser *parser, uint8_t byte,
                struct midi_message *out);

/* --- the ports ------------------------------------------------------------- */

void midi_init(void);

/* Read what has arrived on either port. False when there is nothing. */
bool midi_receive(struct midi_message *out);

void midi_send_note_on(uint8_t note, uint8_t velocity);
void midi_send_note_off(uint8_t note);

/* How long a triggered note is held before it is lifted.
 *
 * sequencer.py sends NoteOn and never NoteOff, which is defensible when the
 * thing on the other end is a drum machine playing one-shots and wrong when it
 * is a synth: those notes hang, and the only way out is a panic message or a
 * power cycle. The samples here are one-shots too, so the length is arbitrary -
 * long enough to read as a hit, short enough that eight of them cannot pile
 * up. */
#define MIDI_NOTE_HOLD_MS 20

/* Send any note-offs that have come due. Called from the main loop. */
void midi_pump(void);
/* Send a clock byte when `frame` reaches the pin, not now.
 *
 * Same reasoning as the sync jack: the sequencer books ticks up to sixteen
 * milliseconds ahead so it can place each hit on an exact frame, and a clock
 * byte sent at that moment arrives that far ahead of the beat it marks. A
 * constant offset is less harmful than jitter - the receiver simply runs early
 * - but it is still wrong, and the machinery to do it properly is already here.
 *
 * Queued rather than a single pending slot: at 300 BPM a tick is 8.3 ms and the
 * lookahead is 16, so two or three can be booked before the first goes out. */
void midi_clock_at_frame(uint64_t frame);

uint32_t midi_clocks_sent(void);
uint32_t midi_clocks_dropped(void);
void midi_send_start(void);
void midi_send_stop(void);

uint32_t midi_bytes_in(void);
uint32_t midi_bytes_out(void);

#endif /* MIDI_H */
