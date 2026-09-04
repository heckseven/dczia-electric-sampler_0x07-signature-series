/* See midi.h. */

#include "midi.h"

/* --- the parser ------------------------------------------------------------ */

static uint8_t data_bytes_for(uint8_t status) {
    switch (status & 0xF0u) {
    case 0xC0u: /* program change */
    case 0xD0u: /* channel pressure */
        return 1;
    default:
        return 2;
    }
}

bool midi_parse(struct midi_parser *parser, uint8_t byte,
                struct midi_message *out) {
    /* System real-time. Handled first and returned immediately, without
     * touching the running status or the half-assembled message: these bytes
     * are defined to be insertable anywhere, including between the two data
     * bytes of a note, and a parser that lets them clear its state drops
     * every note played while a clock is running. */
    if (byte >= 0xF8u) {
        out->channel = 0;
        out->data1 = 0;
        out->data2 = 0;
        switch (byte) {
        case 0xF8u:
            out->kind = MIDI_CLOCK;
            return true;
        case 0xFAu:
            out->kind = MIDI_START;
            return true;
        case 0xFBu:
            out->kind = MIDI_CONTINUE;
            return true;
        case 0xFCu:
            out->kind = MIDI_STOP;
            return true;
        default:
            return false; /* active sensing, reset, undefined */
        }
    }

    if (byte & 0x80u) {
        if (byte >= 0xF0u) {
            /* System common. Ends running status, which the standard requires:
             * a data byte after one of these is not a continuation of the
             * channel message before it. */
            parser->status = 0;
            parser->have = 0;
            return false;
        }
        parser->status = byte;
        parser->have = 0;
        return false;
    }

    if (parser->status == 0) {
        return false; /* data with no status: mid-stream, or noise on the wire */
    }

    parser->data[parser->have++] = byte;
    if (parser->have < data_bytes_for(parser->status)) {
        return false;
    }
    /* Running status: the count resets but the status is kept, so the next
     * pair of data bytes is another message of the same kind. */
    parser->have = 0;

    out->channel = parser->status & 0x0Fu;
    out->data1 = parser->data[0];
    out->data2 = parser->data[1];

    switch (parser->status & 0xF0u) {
    case 0x80u:
        out->kind = MIDI_NOTE_OFF;
        return true;
    case 0x90u:
        /* Velocity zero is note off. Almost everything sends it this way,
         * precisely so running status can be held across both. */
        out->kind = (out->data2 == 0) ? MIDI_NOTE_OFF : MIDI_NOTE_ON;
        return true;
    case 0xB0u:
        out->kind = MIDI_CC;
        return true;
    default:
        return false; /* a channel message this firmware has no use for */
    }
}
