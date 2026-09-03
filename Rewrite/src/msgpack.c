/* Just enough msgpack to read a song the Python wrote.
 *
 * `store.Store` writes maps with string keys and values that are ints, strings,
 * arrays, booleans and byte strings. That is a small corner of the format, and
 * this reads exactly that corner - a full implementation would be several times
 * the size and none of the extra would ever run.
 *
 * Nothing here trusts the file. It came off a card the badge does not control,
 * could have been written by a different version, hand-edited, or truncated by
 * a power cut mid-save. `Song.from_dict` in the Python makes the same
 * assumption and for the same reason: a corrupt file should load as a slightly
 * wrong song, never as a crash on the main loop.
 *
 * Every read is bounds-checked against the end of the buffer, and every reader
 * returns false rather than guessing when the bytes do not say what was
 * expected.
 */

#include <string.h>

#include "msgpack.h"

static bool need(struct mp *mp, uint32_t bytes) {
    return mp->at + bytes <= mp->end;
}

static uint8_t take8(struct mp *mp) {
    return mp->data[mp->at++];
}

static uint16_t take16(struct mp *mp) {
    uint16_t v = (uint16_t)((mp->data[mp->at] << 8) | mp->data[mp->at + 1]);
    mp->at += 2;
    return v;
}

static uint32_t take32(struct mp *mp) {
    uint32_t v = ((uint32_t)mp->data[mp->at] << 24) |
                 ((uint32_t)mp->data[mp->at + 1] << 16) |
                 ((uint32_t)mp->data[mp->at + 2] << 8) |
                 (uint32_t)mp->data[mp->at + 3];
    mp->at += 4;
    return v;
}

void mp_init(struct mp *mp, const uint8_t *data, uint32_t length) {
    mp->data = data;
    mp->at = 0;
    mp->end = length;
}

bool mp_map(struct mp *mp, uint32_t *count) {
    if (!need(mp, 1)) {
        return false;
    }
    uint8_t tag = take8(mp);
    if ((tag & 0xF0) == 0x80) {
        *count = tag & 0x0F;
        return true;
    }
    if (tag == 0xDE && need(mp, 2)) {
        *count = take16(mp);
        return true;
    }
    if (tag == 0xDF && need(mp, 4)) {
        *count = take32(mp);
        return true;
    }
    return false;
}

bool mp_array(struct mp *mp, uint32_t *count) {
    if (!need(mp, 1)) {
        return false;
    }
    uint8_t tag = take8(mp);
    if ((tag & 0xF0) == 0x90) {
        *count = tag & 0x0F;
        return true;
    }
    if (tag == 0xDC && need(mp, 2)) {
        *count = take16(mp);
        return true;
    }
    if (tag == 0xDD && need(mp, 4)) {
        *count = take32(mp);
        return true;
    }
    return false;
}

/* A string or a byte string: both are a length and then that many bytes, and a
 * reader that only wants the bytes has no reason to care which. */
bool mp_bytes(struct mp *mp, const uint8_t **out, uint32_t *length) {
    if (!need(mp, 1)) {
        return false;
    }
    uint8_t tag = take8(mp);
    uint32_t n;

    if ((tag & 0xE0) == 0xA0) {
        n = tag & 0x1F; /* fixstr */
    } else if (tag == 0xD9 || tag == 0xC4) {
        if (!need(mp, 1)) {
            return false;
        }
        n = take8(mp); /* str8 / bin8 */
    } else if (tag == 0xDA || tag == 0xC5) {
        if (!need(mp, 2)) {
            return false;
        }
        n = take16(mp); /* str16 / bin16 */
    } else if (tag == 0xDB || tag == 0xC6) {
        if (!need(mp, 4)) {
            return false;
        }
        n = take32(mp); /* str32 / bin32 */
    } else {
        return false;
    }

    if (!need(mp, n)) {
        return false;
    }
    *out = &mp->data[mp->at];
    *length = n;
    mp->at += n;
    return true;
}

/* Consume a nil if that is what comes next.
 *
 * The Python writes None for a per-track value it has never set - track_volume
 * comes out as [1.5, None, None, ...] - so a reader that expects a number for
 * every entry consumes the wrong number of bytes and every key after it is
 * garbage. Which is exactly what happened: the file parsed correctly up to
 * track_volume and then failed on a key that was not a key. */
bool mp_nil(struct mp *mp) {
    if (need(mp, 1) && mp->data[mp->at] == 0xC0) {
        mp->at++;
        return true;
    }
    return false;
}

bool mp_int(struct mp *mp, int64_t *out) {
    if (!need(mp, 1)) {
        return false;
    }
    uint8_t tag = take8(mp);

    if (tag <= 0x7F) {
        *out = tag; /* positive fixint */
        return true;
    }
    if (tag >= 0xE0) {
        *out = (int8_t)tag; /* negative fixint */
        return true;
    }
    switch (tag) {
    case 0xC2:
        *out = 0; /* false - the Python writes booleans for mutes */
        return true;
    case 0xC3:
        *out = 1; /* true */
        return true;
    case 0xCC:
        if (!need(mp, 1)) {
            return false;
        }
        *out = take8(mp);
        return true;
    case 0xCD:
        if (!need(mp, 2)) {
            return false;
        }
        *out = take16(mp);
        return true;
    case 0xCE:
        if (!need(mp, 4)) {
            return false;
        }
        *out = take32(mp);
        return true;
    case 0xD0:
        if (!need(mp, 1)) {
            return false;
        }
        *out = (int8_t)take8(mp);
        return true;
    case 0xD1:
        if (!need(mp, 2)) {
            return false;
        }
        *out = (int16_t)take16(mp);
        return true;
    case 0xD2:
        if (!need(mp, 4)) {
            return false;
        }
        *out = (int32_t)take32(mp);
        return true;
    default:
        return false;
    }
}

bool mp_float(struct mp *mp, int32_t *milli) {
    /* Track volumes are floats in the Python. Read as a fixed-point
     * thousandth rather than dragging in floating point for one field, and
     * accept an int where a float was expected - 1 and 1.0 mean the same
     * thing to a player. */
    if (!need(mp, 1)) {
        return false;
    }
    uint8_t tag = mp->data[mp->at];
    if (tag == 0xCA) {
        if (!need(mp, 5)) {
            return false;
        }
        mp->at++;
        uint32_t bits = take32(mp);
        /* IEEE-754 single, decoded by hand: no FPU on this chip, and this runs
         * once per song rather than once per sample. */
        int32_t exponent = (int32_t)((bits >> 23) & 0xFF) - 127;
        uint32_t mantissa = (bits & 0x7FFFFF) | 0x800000;
        bool negative = (bits >> 31) != 0;
        int64_t value;
        if (exponent >= 23) {
            value = (int64_t)mantissa << (exponent - 23);
            value *= 1000;
        } else if (exponent >= -23) {
            value = ((int64_t)mantissa * 1000) >> (23 - exponent);
        } else {
            value = 0;
        }
        *milli = (int32_t)(negative ? -value : value);
        return true;
    }
    if (tag == 0xCB) {
        /* Doubles are not written by this firmware but cost four lines to
         * skip rather than fail on. */
        if (!need(mp, 9)) {
            return false;
        }
        mp->at += 9;
        *milli = 1000;
        return true;
    }

    int64_t whole;
    if (mp_int(mp, &whole)) {
        *milli = (int32_t)(whole * 1000);
        return true;
    }
    return false;
}

/* Step over one value of any type, so an unrecognised key can be ignored
 * instead of derailing the whole file. */
bool mp_skip(struct mp *mp) {
    if (!need(mp, 1)) {
        return false;
    }
    uint8_t tag = mp->data[mp->at];

    if (tag <= 0x7F || tag >= 0xE0 || tag == 0xC0 || tag == 0xC2 ||
        tag == 0xC3) {
        mp->at++;
        return true;
    }
    if ((tag & 0xE0) == 0xA0 || tag == 0xD9 || tag == 0xDA || tag == 0xDB ||
        tag == 0xC4 || tag == 0xC5 || tag == 0xC6) {
        const uint8_t *bytes;
        uint32_t length;
        return mp_bytes(mp, &bytes, &length);
    }
    if ((tag & 0xF0) == 0x90 || tag == 0xDC || tag == 0xDD) {
        uint32_t count;
        if (!mp_array(mp, &count)) {
            return false;
        }
        for (uint32_t i = 0; i < count; i++) {
            if (!mp_skip(mp)) {
                return false;
            }
        }
        return true;
    }
    if ((tag & 0xF0) == 0x80 || tag == 0xDE || tag == 0xDF) {
        uint32_t count;
        if (!mp_map(mp, &count)) {
            return false;
        }
        for (uint32_t i = 0; i < count * 2; i++) {
            if (!mp_skip(mp)) {
                return false;
            }
        }
        return true;
    }
    if (tag == 0xCA) {
        return need(mp, 5) ? (mp->at += 5, true) : false;
    }
    if (tag == 0xCB) {
        return need(mp, 9) ? (mp->at += 9, true) : false;
    }

    int64_t discard;
    return mp_int(mp, &discard);
}

bool mp_key_is(const uint8_t *key, uint32_t length, const char *name) {
    return strlen(name) == length && memcmp(key, name, length) == 0;
}
