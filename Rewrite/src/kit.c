/* See kit.h. */

#include <string.h>

#include "audio.h"
#include "fat.h"
#include "kit.h"

/* Read in chunks rather than a sector at a time: fat_read already works from a
 * cached sector, and a larger request means fewer calls without a second copy.
 * 512 int16 is 1 KB of stack, which core 0 has. */
#define CHUNK_FRAMES 512

static uint32_t le32_of(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint16_t le16_of(const uint8_t *p) {
    return (uint16_t)(p[0] | (p[1] << 8));
}

enum kit_result kit_load_track(uint8_t track, const char *path,
                               uint32_t *frames_out) {
    *frames_out = 0;

    struct fat_file file;
    if (!fat_open(path, &file)) {
        return KIT_NO_FILE;
    }

    uint8_t header[12];
    if (fat_read(&file, header, sizeof(header)) != sizeof(header) ||
        memcmp(header, "RIFF", 4) != 0 || memcmp(&header[8], "WAVE", 4) != 0) {
        return KIT_NOT_WAV;
    }

    /* Walk the chunks. `fmt ` and `data` are not required to be adjacent or in
     * order, and files written by a converter often carry a LIST chunk between
     * them - assuming the layout is how a reader ends up parsing metadata as
     * audio. */
    bool have_format = false;
    uint32_t data_bytes = 0;
    for (;;) {
        uint8_t chunk[8];
        if (fat_read(&file, chunk, sizeof(chunk)) != sizeof(chunk)) {
            return KIT_NOT_WAV;
        }
        uint32_t size = le32_of(&chunk[4]);

        if (memcmp(chunk, "fmt ", 4) == 0) {
            uint8_t fmt[16];
            if (size < sizeof(fmt) ||
                fat_read(&file, fmt, sizeof(fmt)) != sizeof(fmt)) {
                return KIT_NOT_WAV;
            }
            uint16_t format = le16_of(&fmt[0]);
            uint16_t channels = le16_of(&fmt[2]);
            uint32_t rate = le32_of(&fmt[4]);
            uint16_t bits = le16_of(&fmt[14]);
            if (format != 1 || channels != CHANNELS || rate != SAMPLE_RATE ||
                bits != BITS_PER_SAMPLE) {
                return KIT_WRONG_FORMAT;
            }
            have_format = true;
            /* Skip any extension bytes past the 16 read. */
            for (uint32_t skipped = sizeof(fmt); skipped < size; skipped++) {
                uint8_t discard;
                if (fat_read(&file, &discard, 1) != 1) {
                    return KIT_NOT_WAV;
                }
            }
        } else if (memcmp(chunk, "data", 4) == 0) {
            data_bytes = size;
            break;
        } else {
            for (uint32_t skipped = 0; skipped < size; skipped++) {
                uint8_t discard;
                if (fat_read(&file, &discard, 1) != 1) {
                    return KIT_NOT_WAV;
                }
            }
        }
        /* Chunks are word-aligned; an odd size carries a pad byte. */
        if (size & 1) {
            uint8_t pad;
            fat_read(&file, &pad, 1);
        }
    }

    if (!have_format) {
        return KIT_NOT_WAV;
    }

    /* A data chunk can claim more than the file holds. Trust the shorter. */
    uint32_t available = file.size - file.position;
    if (data_bytes > available) {
        data_bytes = available;
    }
    uint32_t frames = data_bytes / sizeof(int16_t);
    if (frames < 2) {
        return KIT_WRONG_FORMAT;
    }

    int16_t *destination = audio_arena_alloc(frames);
    if (destination == NULL) {
        return KIT_NO_ROOM;
    }

    /* Straight into the arena. The samples are little-endian 16-bit and so is
     * this chip, so there is nothing to convert - which is why the card holds
     * them in exactly this format. */
    uint32_t loaded = 0;
    while (loaded < frames) {
        uint32_t want = frames - loaded;
        if (want > CHUNK_FRAMES) {
            want = CHUNK_FRAMES;
        }
        uint32_t got = fat_read(&file, &destination[loaded],
                                want * sizeof(int16_t));
        if (got == 0) {
            break;
        }
        loaded += got / sizeof(int16_t);
    }

    if (loaded < 2) {
        return KIT_WRONG_FORMAT;
    }

    audio_set_sample(track, destination, loaded);
    *frames_out = loaded;
    return KIT_OK;
}

const char *kit_result_name(enum kit_result result) {
    switch (result) {
    case KIT_OK:
        return "ok";
    case KIT_NO_FILE:
        return "no_file";
    case KIT_NOT_WAV:
        return "not_wav";
    case KIT_WRONG_FORMAT:
        return "wrong_format";
    case KIT_NO_ROOM:
        return "no_room";
    }
    return "unknown";
}
