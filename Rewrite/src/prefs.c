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
    memset(prefs, 0, sizeof(*prefs));
    prefs->volume = PREFS_NO_VOLUME;
    carried_count = 0;

    struct fat_file file;
    if (!fat_open(PREFS_PATH, &file) || file.size == 0 ||
        file.size > PREFS_MAX_BYTES) {
        return;
    }
    uint32_t got = fat_read(&file, buffer, file.size);
    if (got != file.size) {
        return;
    }

    struct mp mp;
    mp_init(&mp, buffer, got);
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
                memcpy(carried[carried_count].value, &buffer[start], length);
                carried[carried_count].length = length;
                carried_count++;
            }
        }
    }
    prefs->loaded = true;
}

bool prefs_save(const struct prefs *prefs) {
    struct mpw w;
    mpw_init(&w, buffer, sizeof(buffer));
    mpw_map(&w, 2 + carried_count);

    mpw_str(&w, "song");
    mpw_str(&w, prefs->song);

    mpw_str(&w, "volume");
    mpw_int(&w, prefs->volume);

    for (uint32_t i = 0; i < carried_count; i++) {
        mpw_str(&w, carried[i].key);
        mpw_raw(&w, carried[i].value, carried[i].length);
    }

    if (!w.ok) {
        return false;
    }
    return fat_write(PREFS_PATH, buffer, w.at);
}
