/* Host tests for the MIDI parser.
 *
 * A MIDI stream is not a sequence of self-contained messages, and the three
 * ways it is not are the three ways a parser fails against real gear while
 * passing against a test program that sends tidy three-byte notes.
 */

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "midi.h"

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

/* Feed a stream, collect everything that comes out. */
static struct midi_message got[64];
static uint32_t got_count;

static void feed(const uint8_t *bytes, uint32_t length) {
    static struct midi_parser parser;
    memset(&parser, 0, sizeof(parser));
    got_count = 0;
    struct midi_message m;
    for (uint32_t i = 0; i < length; i++) {
        if (midi_parse(&parser, bytes[i], &m) && got_count < 64) {
            got[got_count++] = m;
        }
    }
}

static void test_a_plain_note(void) {
    const uint8_t stream[] = {0x90, 60, 100, 0x80, 60, 64};
    feed(stream, sizeof(stream));
    check(got_count == 2, "two messages");
    check(got[0].kind == MIDI_NOTE_ON, "note on");
    check(got[0].data1 == 60 && got[0].data2 == 100, "with note and velocity");
    check(got[0].channel == 0, "on channel 1");
    check(got[1].kind == MIDI_NOTE_OFF, "and note off");
}

static void test_velocity_zero_is_note_off(void) {
    /* Almost everything sends note off this way, precisely so running status
     * can be held across both. A parser that only knows 0x80 hangs every note
     * such a keyboard plays. */
    const uint8_t stream[] = {0x90, 60, 100, 0x90, 60, 0};
    feed(stream, sizeof(stream));
    check(got_count == 2, "two messages");
    check(got[0].kind == MIDI_NOTE_ON, "the first is on");
    check(got[1].kind == MIDI_NOTE_OFF, "the second is off, not a second on");
    check(got[1].data1 == 60, "for the same note");
}

static void test_running_status(void) {
    /* One status byte, then pairs of data bytes for as long as it likes. A
     * keyboard playing a fast trill sends exactly this. */
    const uint8_t stream[] = {0x90, 60, 100, 62, 100, 64, 100, 60, 0};
    feed(stream, sizeof(stream));
    check(got_count == 4, "four messages from one status byte");
    check(got[0].data1 == 60 && got[1].data1 == 62 && got[2].data1 == 64,
          "each carrying its own note");
    check(got[3].kind == MIDI_NOTE_OFF, "and the last one lifts the first");
}

static void test_clock_between_the_data_bytes_of_a_note(void) {
    /* The case that matters most, and the one a tidy test never reaches.
     *
     * Real-time bytes may appear between ANY two bytes. Clock is 0xF8 and
     * arrives 24 times a quarter note, so at any real tempo this happens
     * constantly - and a parser that lets 0xF8 clear its state drops every
     * note played while a master is running. */
    const uint8_t stream[] = {0x90, 60, 0xF8, 100};
    feed(stream, sizeof(stream));
    check(got_count == 2, "the clock and the note both come out");
    check(got[0].kind == MIDI_CLOCK, "the clock arrives first, mid-note");
    check(got[1].kind == MIDI_NOTE_ON, "and the note completes around it");
    check(got[1].data1 == 60 && got[1].data2 == 100,
          "with both its data bytes intact");
}

static void test_clock_does_not_break_running_status(void) {
    /* Same hazard, one level out: a clock between two running-status messages
     * must not end the run. */
    const uint8_t stream[] = {0x90, 60, 100, 0xF8, 62, 100, 0xF8, 0xF8, 64, 100};
    feed(stream, sizeof(stream));
    uint32_t notes = 0, clocks = 0;
    for (uint32_t i = 0; i < got_count; i++) {
        if (got[i].kind == MIDI_NOTE_ON) {
            notes++;
        } else if (got[i].kind == MIDI_CLOCK) {
            clocks++;
        }
    }
    check(notes == 3, "all three notes survive the clocks between them");
    check(clocks == 3, "and all three clocks are reported");
}

static void test_system_common_ends_running_status(void) {
    /* Unlike real-time. A data byte after song-position is not a continuation
     * of the note that came before it, and treating it as one invents a note
     * nobody played. */
    const uint8_t stream[] = {0x90, 60, 100, 0xF2, 0x00, 0x04, 62, 100};
    feed(stream, sizeof(stream));
    check(got_count == 1, "only the real note comes out");
    check(got[0].data1 == 60, "which is the one that was actually sent");
}

static void test_transport_bytes(void) {
    const uint8_t stream[] = {0xFA, 0xF8, 0xFB, 0xFC, 0xFE};
    feed(stream, sizeof(stream));
    check(got_count == 4, "active sensing is ignored, the rest are not");
    check(got[0].kind == MIDI_START, "start");
    check(got[1].kind == MIDI_CLOCK, "clock");
    check(got[2].kind == MIDI_CONTINUE, "continue");
    check(got[3].kind == MIDI_STOP, "stop");
}

static void test_data_before_any_status_is_dropped(void) {
    /* Joining a stream part way through, or noise on an unshielded cable.
     * Inventing a message from it would fire a note nobody played. */
    const uint8_t stream[] = {60, 100, 62, 0x90, 64, 100};
    feed(stream, sizeof(stream));
    check(got_count == 1, "only the message with a status byte");
    check(got[0].data1 == 64, "and it is the right one");
}

static void test_two_byte_messages(void) {
    /* Program change and channel pressure carry one data byte, not two.
     * Reading two would swallow the next status byte and desynchronise
     * everything after it. */
    const uint8_t stream[] = {0xC0, 5, 0x90, 60, 100};
    feed(stream, sizeof(stream));
    check(got_count == 1, "the program change is skipped, not mis-sized");
    check(got[0].kind == MIDI_NOTE_ON && got[0].data1 == 60,
          "and the note after it parses correctly");
}

int main(void) {
    test_a_plain_note();
    test_velocity_zero_is_note_off();
    test_running_status();
    test_clock_between_the_data_bytes_of_a_note();
    test_clock_does_not_break_running_status();
    test_system_common_ends_running_status();
    test_transport_bytes();
    test_data_before_any_status_is_dropped();
    test_two_byte_messages();

    if (failures == 0) {
        printf("ok - all MIDI tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
