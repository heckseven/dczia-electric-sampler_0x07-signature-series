/* See prefs.h. */

#include <string.h>

#include "fat.h"
#include "msgpack.h"
#include "prefs.h"

/* Small: the Python bounds this file at a few hundred bytes and nothing here
 * writes more than that. */
#define PREFS_MAX_BYTES 512
static uint8_t buffer[PREFS_MAX_BYTES];

/* Keys read but not modelled, kept verbatim so writing this file does not
 * discard settings that belong to the other firmware. Each is a copy of the
 * encoded value, because re-encoding something this build does not understand
 * is how a round trip quietly changes it. */
#define CARRIED_MAX 4
struct carried {
    char key[16];
    uint8_t value[64];
    uint32_t length;
};
static struct carried carried[CARRIED_MAX];
static uint32_t carried_count;

void prefs_load(struct prefs *prefs) {
    struct fat_file file;
    if (!fat_open(PREFS_PATH, &file) || file.size == 0 ||
        file.size > PREFS_MAX_BYTES) {
        prefs_decode(NULL, 0, prefs);
        return;
    }
    uint32_t got = fat_read(&file, buffer, file.size);
    if (got != file.size) {
        prefs_decode(NULL, 0, prefs);
        return;
    }
    prefs_decode(buffer, got, prefs);
}

void prefs_decode(const uint8_t *data, uint32_t length, struct prefs *prefs) {
    memset(prefs, 0, sizeof(*prefs));
    prefs->volume = PREFS_NO_VOLUME;
    prefs->brightness = PREFS_BRIGHTNESS_DEFAULT;
    carried_count = 0;
    if (data == NULL || length == 0) {
        return;
    }

    struct mp mp;
    mp_init(&mp, data, length);
    uint32_t pairs;
    if (!mp_map(&mp, &pairs)) {
        return;
    }

    for (uint32_t i = 0; i < pairs; i++) {
        const uint8_t *key;
        uint32_t key_length;
        if (!mp_bytes(&mp, &key, &key_length)) {
            return;
        }

        if (mp_key_is(key, key_length, "song")) {
            const uint8_t *name;
            uint32_t length;
            if (mp_bytes(&mp, &name, &length)) {
                uint32_t n = length < PREFS_NAME_MAX - 1 ? length
                                                         : PREFS_NAME_MAX - 1;
                memcpy(prefs->song, name, n);
                prefs->song[n] = '\0';
            }
        } else if (mp_key_is(key, key_length, "brightness")) {
            int64_t value;
            if (mp_int(&mp, &value)) {
                /* Clamped on the way in as well as on the way out. A file
                 * edited by hand, or written by a future version with a higher
                 * ceiling, must not be able to set a brightness this board
                 * cannot power. */
                if (value < PREFS_BRIGHTNESS_MIN) {
                    value = PREFS_BRIGHTNESS_MIN;
                }
                if (value > PREFS_BRIGHTNESS_MAX) {
                    value = PREFS_BRIGHTNESS_MAX;
                }
                prefs->brightness = (uint8_t)value;
            }
        } else if (mp_key_is(key, key_length, "volume")) {
            int64_t value;
            if (mp_nil(&mp)) {
                prefs->volume = PREFS_NO_VOLUME;
            } else if (mp_int(&mp, &value)) {
                prefs->volume = (int32_t)value;
            }
        } else {
            /* Remember it exactly as encoded, so a later save gives it back. */
            uint32_t start = mp.at;
            if (!mp_skip(&mp)) {
                return;
            }
            uint32_t length = mp.at - start;
            if (carried_count < CARRIED_MAX && key_length < 16 &&
                length <= sizeof(carried[0].value)) {
                memcpy(carried[carried_count].key, key, key_length);
                carried[carried_count].key[key_length] = '\0';
                /* From `data`, not from the module's own buffer. They are the
                 * same pointer when prefs_load calls this, which is why
                 * reading the wrong one worked for as long as that was the
                 * only caller - and would have gone on working right up until
                 * it was not. */
                memcpy(carried[carried_count].value, &data[start], length);
                carried[carried_count].length = length;
                carried_count++;
            }
        }
    }
    prefs->loaded = true;
}

bool prefs_save(const struct prefs *prefs) {
    uint32_t length;
    if (!prefs_encode(buffer, sizeof(buffer), prefs, &length)) {
        return false;
    }
    return fat_write(PREFS_PATH, buffer, length);
}

bool prefs_encode(uint8_t *out, uint32_t capacity, const struct prefs *prefs,
                  uint32_t *length_out) {
    struct mpw w;
    mpw_init(&w, out, capacity);
    mpw_map(&w, 3 + carried_count); /* song, brightness, volume */

    mpw_str(&w, "song");
    mpw_str(&w, prefs->song);

    mpw_str(&w, "brightness");
    mpw_int(&w, prefs->brightness);

    mpw_str(&w, "volume");
    mpw_int(&w, prefs->volume);

    for (uint32_t i = 0; i < carried_count; i++) {
        mpw_str(&w, carried[i].key);
        mpw_raw(&w, carried[i].value, carried[i].length);
    }

    if (!w.ok) {
        return false;
    }
    if (!w.ok) {
        return false;
    }
    *length_out = w.at;
    return true;
}
