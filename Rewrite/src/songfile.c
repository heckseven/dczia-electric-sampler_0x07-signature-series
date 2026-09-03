/* Reading a song the Python wrote.
 *
 * The shape is Song.to_dict: a map of "length", "lengths", "division", "bpm",
 * "muted", "track_volume", "steps" and "offsets", where steps and offsets are
 * eight byte strings of 64 bytes each.
 *
 * Every field is clamped rather than trusted, which is what the Python does and
 * for the same reason. An unknown key is skipped rather than refused, so a song
 * written by a later version loses the fields this build does not understand
 * and keeps the ones it does.
 */

#include <string.h>

#include "fat.h"
#include "msgpack.h"
#include "song.h"
#include "songfile.h"

/* A song is about 1.2 KB packed. 4 KB is generous and still small against the
 * arena; a file bigger than this is not one this firmware wrote. */
#define SONG_MAX_BYTES 4096
static uint8_t buffer[SONG_MAX_BYTES];

static void read_byte_rows(struct mp *mp, uint8_t rows[TRACK_COUNT][MAX_STEPS],
                           uint8_t fill) {
    uint32_t count;
    if (!mp_array(mp, &count)) {
        return;
    }
    for (uint32_t t = 0; t < count; t++) {
        if (mp_nil(mp)) {
            continue;
        }
        const uint8_t *row;
        uint32_t length;
        if (!mp_bytes(mp, &row, &length)) {
            return;
        }
        if (t >= TRACK_COUNT) {
            continue; /* a file with more tracks than this build has */
        }
        for (uint32_t s = 0; s < MAX_STEPS; s++) {
            rows[t][s] = (s < length) ? row[s] : fill;
        }
    }
}

static void read_int_row(struct mp *mp, uint8_t *row, uint32_t width,
                         int32_t low, int32_t high) {
    uint32_t count;
    if (!mp_array(mp, &count)) {
        return;
    }
    for (uint32_t i = 0; i < count; i++) {
        if (mp_nil(mp)) {
            continue; /* never set - keep the default */
        }
        int64_t value;
        if (!mp_int(mp, &value)) {
            return;
        }
        if (i < width) {
            if (value < low) {
                value = low;
            }
            if (value > high) {
                value = high;
            }
            row[i] = (uint8_t)value;
        }
    }
}

enum songfile_result songfile_load(const char *path, struct song *song) {
    struct fat_file file;
    if (!fat_open(path, &file)) {
        return SONGFILE_NO_FILE;
    }
    if (file.size == 0 || file.size > SONG_MAX_BYTES) {
        return SONGFILE_TOO_BIG;
    }
    uint32_t got = fat_read(&file, buffer, file.size);
    if (got != file.size) {
        return SONGFILE_SHORT;
    }

    /* Start from a valid song, so anything the file does not mention keeps a
     * sensible default rather than whatever was in memory. */
    song_init(song);

    struct mp mp;
    mp_init(&mp, buffer, got);

    uint32_t pairs;
    if (!mp_map(&mp, &pairs)) {
        return SONGFILE_NOT_A_SONG;
    }

    for (uint32_t i = 0; i < pairs; i++) {
        const uint8_t *key;
        uint32_t key_length;
        if (!mp_bytes(&mp, &key, &key_length)) {
            /* A key that is not a string means a previous value consumed the
             * wrong number of bytes. That is the failure mode this format has:
             * one misread value and every key after it is garbage. */
            return SONGFILE_NOT_A_SONG;
        }

        if (mp_key_is(key, key_length, "bpm")) {
            int64_t value;
            if (mp_int(&mp, &value)) {
                song_set_bpm(song, (int32_t)value);
            }
        } else if (mp_key_is(key, key_length, "division")) {
            int64_t value;
            if (mp_int(&mp, &value)) {
                song_set_division(song, (int32_t)value);
            }
        } else if (mp_key_is(key, key_length, "lengths")) {
            const uint8_t *row;
            uint32_t length;
            if (mp_bytes(&mp, &row, &length)) {
                for (uint32_t t = 0; t < TRACK_COUNT && t < length; t++) {
                    song_set_length(song, (uint8_t)t, row[t]);
                }
            }
        } else if (mp_key_is(key, key_length, "muted")) {
            read_int_row(&mp, song->muted, TRACK_COUNT, 0, 1);
        } else if (mp_key_is(key, key_length, "steps")) {
            read_byte_rows(&mp, song->steps, VELOCITY_OFF);
        } else if (mp_key_is(key, key_length, "offsets")) {
            read_byte_rows(&mp, song->offsets, OFFSET_BIAS);
        } else if (mp_key_is(key, key_length, "track_volume")) {
            uint32_t count;
            if (mp_array(&mp, &count)) {
                for (uint32_t t = 0; t < count; t++) {
                    if (mp_nil(&mp)) {
                        continue; /* never set - keep the default of 1.0 */
                    }
                    int32_t milli;
                    if (!mp_float(&mp, &milli)) {
                        break;
                    }
                    if (t < TRACK_COUNT) {
                        /* 0.0 to 2.0 in the Python, held here as Q12. */
                        if (milli < 0) {
                            milli = 0;
                        }
                        if (milli > 2000) {
                            milli = 2000;
                        }
                        song->volume_q12[t] = (uint16_t)((milli * 4096) / 1000);
                    }
                }
            }
        } else if (!mp_skip(&mp)) {
            /* An unknown key whose value cannot even be stepped over. */
            /* "length", "kit", "kit_name", "kit_volume", "track_strength", "v" -
             * all meaningful, none of them Phase 2's. Skipped rather than
             * refused, so a song keeps the parts this build understands. */
            return SONGFILE_NOT_A_SONG;
        }
    }

    /* The division may have been read after the offsets, which would leave
     * offsets legal for the old division and not the new one. */
    song_set_division(song, song->division);
    return SONGFILE_OK;
}

const char *songfile_result_name(enum songfile_result result) {
    switch (result) {
    case SONGFILE_OK:
        return "ok";
    case SONGFILE_NO_FILE:
        return "no_file";
    case SONGFILE_TOO_BIG:
        return "too_big";
    case SONGFILE_SHORT:
        return "short_read";
    case SONGFILE_NOT_A_SONG:
        return "not_a_song";
    }
    return "unknown";
}
