/* A small msgpack reader: maps, arrays, ints, floats, strings, byte strings.
 *
 * Enough to read what store.Store writes, and no more. Nothing here trusts the
 * bytes - they came off a card the badge does not control, and a file that is
 * corrupt, truncated or written by another version must load as a slightly
 * wrong song rather than as a crash.
 */

#ifndef MSGPACK_H
#define MSGPACK_H

#include <stdbool.h>
#include <stdint.h>

struct mp {
    const uint8_t *data;
    uint32_t at;
    uint32_t end;
};

void mp_init(struct mp *mp, const uint8_t *data, uint32_t length);

bool mp_map(struct mp *mp, uint32_t *count);
bool mp_array(struct mp *mp, uint32_t *count);
bool mp_bytes(struct mp *mp, const uint8_t **out, uint32_t *length);
bool mp_int(struct mp *mp, int64_t *out);

/* Consume a nil if that is what comes next. The Python writes None for
 * per-track values it has never set, and a reader that insists on a number
 * there desynchronises the rest of the file. */
bool mp_nil(struct mp *mp);

/* Floats as thousandths, so track volumes can be read without an FPU. */
bool mp_float(struct mp *mp, int32_t *milli);

/* Step over any one value, so an unknown key is ignored rather than fatal. */
bool mp_skip(struct mp *mp);

bool mp_key_is(const uint8_t *key, uint32_t length, const char *name);

/* --- writing --------------------------------------------------------------- */

struct mpw {
    uint8_t *data;
    uint32_t at;
    uint32_t end;
    /* Cleared the first time anything would overflow, and never set again, so
     * one check at the end covers the whole document. */
    bool ok;
};

void mpw_init(struct mpw *w, uint8_t *buffer, uint32_t capacity);
void mpw_map(struct mpw *w, uint32_t count);
void mpw_array(struct mpw *w, uint32_t count);
void mpw_str(struct mpw *w, const char *text);
void mpw_bin(struct mpw *w, const uint8_t *bytes, uint32_t n);
void mpw_int(struct mpw *w, int32_t value);
void mpw_bool(struct mpw *w, bool value);
void mpw_nil(struct mpw *w);

/* A float, given as thousandths. The Python reads floats for track volumes, and
 * writing an integer there would work today and break on a version that stops
 * being tolerant. */
void mpw_float_milli(struct mpw *w, int32_t milli);

#endif /* MSGPACK_H */
