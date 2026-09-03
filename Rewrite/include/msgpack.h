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

#endif /* MSGPACK_H */
