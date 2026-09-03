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

/* --- writing --------------------------------------------------------------- *
 *
 * The mirror of the reader, and only as much of it. Everything written here has
 * to be readable by CircuitPython's msgpack, which is the test that matters:
 * a file this firmware can read back but the Python cannot is a file that has
 * stranded the player's work in a rewrite that was supposed to preserve it.
 */

void mpw_init(struct mpw *w, uint8_t *buffer, uint32_t capacity) {
    w->data = buffer;
    w->at = 0;
    w->end = capacity;
    w->ok = true;
}

static void emit(struct mpw *w, uint8_t byte) {
    if (!w->ok || w->at >= w->end) {
        w->ok = false;
        return;
    }
    w->data[w->at++] = byte;
}

static void emit_bytes(struct mpw *w, const uint8_t *bytes, uint32_t n) {
    if (!w->ok || w->at + n > w->end) {
        w->ok = false;
        return;
    }
    memcpy(&w->data[w->at], bytes, n);
    w->at += n;
}

void mpw_map(struct mpw *w, uint32_t count) {
    if (count < 16) {
        emit(w, (uint8_t)(0x80 | count));
    } else {
        emit(w, 0xDE);
        emit(w, (uint8_t)(count >> 8));
        emit(w, (uint8_t)count);
    }
}

void mpw_array(struct mpw *w, uint32_t count) {
    if (count < 16) {
        emit(w, (uint8_t)(0x90 | count));
    } else {
        emit(w, 0xDC);
        emit(w, (uint8_t)(count >> 8));
        emit(w, (uint8_t)count);
    }
}

void mpw_str(struct mpw *w, const char *text) {
    uint32_t n = (uint32_t)strlen(text);
    if (n < 32) {
        emit(w, (uint8_t)(0xA0 | n));
    } else {
        emit(w, 0xD9);
        emit(w, (uint8_t)n);
    }
    emit_bytes(w, (const uint8_t *)text, n);
}

void mpw_bin(struct mpw *w, const uint8_t *bytes, uint32_t n) {
    emit(w, 0xC4);
    emit(w, (uint8_t)n);
    emit_bytes(w, bytes, n);
}

void mpw_int(struct mpw *w, int32_t value) {
    if (value >= 0 && value < 128) {
        emit(w, (uint8_t)value);
    } else if (value < 0 && value >= -32) {
        emit(w, (uint8_t)value);
    } else if (value >= 0 && value < 256) {
        emit(w, 0xCC);
        emit(w, (uint8_t)value);
    } else if (value >= 0 && value < 65536) {
        emit(w, 0xCD);
        emit(w, (uint8_t)(value >> 8));
        emit(w, (uint8_t)value);
    } else {
        emit(w, 0xD2);
        emit(w, (uint8_t)(value >> 24));
        emit(w, (uint8_t)(value >> 16));
        emit(w, (uint8_t)(value >> 8));
        emit(w, (uint8_t)value);
    }
}

void mpw_bool(struct mpw *w, bool value) {
    emit(w, value ? 0xC3 : 0xC2);
}

void mpw_nil(struct mpw *w) {
    emit(w, 0xC0);
}

void mpw_float_milli(struct mpw *w, int32_t milli) {
    /* Build an IEEE-754 single by hand. No FPU on this chip, and the Python
     * reads floats here - writing an integer where it expects a float would
     * work today and is exactly the kind of near-miss that breaks on a version
     * that stops being tolerant. */
    emit(w, 0xCA);
    if (milli == 0) {
        emit(w, 0);
        emit(w, 0);
        emit(w, 0);
        emit(w, 0);
        return;
    }
    bool negative = milli < 0;
    uint32_t magnitude = (uint32_t)(negative ? -milli : milli);

    /* value = magnitude / 1000, normalised to 1.xxx * 2^exponent. */
    int32_t exponent = 0;
    uint64_t scaled = (uint64_t)magnitude << 24; /* headroom for the mantissa */
    uint64_t divided = scaled / 1000u;
    while (divided >= (1ull << 24)) {
        divided >>= 1;
        exponent++;
    }
    while (divided < (1ull << 23)) {
        divided <<= 1;
        exponent--;
    }
    /* Where the exponent comes from, written out because getting it wrong is
     * silent: the encoder produced exactly twice the value, which reads back as
     * a plausible volume rather than as an error.
     *
     *   divided_initial = value * 2^24            (magnitude << 24, over 1000)
     *   divided_final   = divided_initial / 2^exponent   (the two loops)
     *   divided_final   = m * 2^23, with m in [1, 2)
     *
     * so value = m * 2^(exponent - 1), and IEEE wants the exponent of m. */
    int32_t biased = exponent - 1 + 127;
    uint32_t mantissa = (uint32_t)(divided & 0x7FFFFF);
    uint32_t bits = (negative ? 0x80000000u : 0u) |
                    ((uint32_t)(biased & 0xFF) << 23) | mantissa;
    emit(w, (uint8_t)(bits >> 24));
    emit(w, (uint8_t)(bits >> 16));
    emit(w, (uint8_t)(bits >> 8));
    emit(w, (uint8_t)bits);
}
