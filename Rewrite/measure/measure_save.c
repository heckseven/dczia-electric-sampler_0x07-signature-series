/* Phase 3: does a saved song survive, and does overwriting one leave the card
 * intact?
 *
 * The write ordering in fat_write is the claim - data, then chain, then one
 * directory entry, then release the old chain - and the part of it that can be
 * checked without pulling a cable is that the file is correct afterwards and
 * that doing it twice does not corrupt anything.
 *
 * Two rounds on purpose. The first creates a file, which takes the "no entry
 * yet, find a free slot" path. The second overwrites it, which takes the
 * "rewrite the entry, free the old chain" path - and that is the one that can
 * cross-link a filesystem if the ordering is wrong.
 */

#include <stdio.h>
#include <string.h>

#include "pico/stdlib.h"

#include "console.h"
#include "fat.h"
#include "sd.h"
#include "song.h"
#include "songfile.h"

#define TEST_PATH "/songs/csave.song"

static struct song written;
static struct song read_back;

static uint32_t failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("RESULT case=save FAIL %s\n", what);
        failures++;
    }
}

/* Fill a song with values that are all different from the defaults, so a field
 * that silently fails to round-trip cannot look correct by accident. */
static void fill(struct song *song, uint32_t round) {
    song_init(song);
    song_set_bpm(song, (int32_t)(90 + round * 37));
    song_set_division(song, (int32_t)(round % DIVISION_COUNT));
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(song, (uint8_t)t, 3 + t + round);
        song->muted[t] = (uint8_t)((t + round) & 1);
        song->volume_q12[t] = (uint16_t)(2048 + t * 512);
    }
    /* Real sample paths, so the /sd prefix translation is exercised rather
     * than assumed: the Python writes "/sd/samples/x.wav" and this firmware
     * mounts the card at the root, so a song has to survive the trip in both
     * directions to mean the same thing to both. */
    static const char *PATHS[] = {
        "/samples/kick_crater.wav",
        "/sd/samples/snare_kraken-head_1.wav",
        "/samples/hh_hats-closed_1.wav",
        "",
        "/samples/fx_lucifer.wav",
        "",
        "/sd/samples/1001.wav",
        "/samples/cymbals_crucible-edge_1.wav",
    };
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_kit_path(song, (uint8_t)t,
                          PATHS[(t + round) % count_of(PATHS)]);
    }

    int32_t limit = song_max_offset(song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        for (uint32_t s = 0; s < song->lengths[t]; s++) {
            if (((s + t + round) % 3) == 0) {
                int32_t offset = ((int32_t)(s % 3) - 1);
                if (offset > limit) {
                    offset = limit;
                }
                if (offset < -limit) {
                    offset = -limit;
                }
                song_set_step(song, (uint8_t)t, s,
                              (uint8_t)(20 + (s * 7 + t) % 100), offset);
            }
        }
    }
}

static void compare(const struct song *a, const struct song *b) {
    check(a->bpm == b->bpm, "bpm");
    check(a->division == b->division, "division");
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        check(a->lengths[t] == b->lengths[t], "length");
        check(strcmp(a->kit[t], b->kit[t]) == 0, "kit path");
        check(a->muted[t] == b->muted[t], "muted");
        /* Volume goes out as thousandths of a float and comes back as Q12, so
         * it cannot be expected to be bit-exact. One part in 4096 is well
         * under what a knob can set or an ear can hear. */
        int32_t difference = (int32_t)a->volume_q12[t] - b->volume_q12[t];
        check(difference > -8 && difference < 8, "volume");
        for (uint32_t s = 0; s < MAX_STEPS; s++) {
            check(a->steps[t][s] == b->steps[t][s], "step");
            check(a->offsets[t][s] == b->offsets[t][s], "offset");
        }
    }
}

int main(void) {
    console_begin("rt-save");

    bool card = sd_init();
    bool mounted = card && fat_mount();
    printf("RESULT case=save card=%d mounted=%d\n", card ? 1 : 0,
           mounted ? 1 : 0);
    if (!mounted) {
        printf("DONE spike=rt-save\n");
        while (true) {
            console_pump();
            sleep_ms(10);
        }
    }

    fat_delete(TEST_PATH);
    /* Tidy up after the prefs test, which deliberately left its output behind
     * so the bytes could be looked at. It has been looked at. */
    if (fat_delete("/settings.test")) {
        printf("RESULT case=save removed=/settings.test\n");
    }

    for (uint32_t round = 1; round <= 2; round++) {
        fill(&written, round);

        absolute_time_t started = get_absolute_time();
        enum songfile_result saved = songfile_save(TEST_PATH, &written);
        int64_t save_us = absolute_time_diff_us(started, get_absolute_time());
        check(saved == SONGFILE_OK, "save succeeded");

        enum songfile_result loaded = songfile_load(TEST_PATH, &read_back);
        check(loaded == SONGFILE_OK, "load succeeded");
        if (saved == SONGFILE_OK && loaded == SONGFILE_OK) {
            compare(&written, &read_back);
        }

        printf("RESULT case=save round=%lu saved=%s loaded=%s save_us=%lld "
               "bpm=%u->%u failures=%lu\n",
               (unsigned long)round, songfile_result_name(saved),
               songfile_result_name(loaded), (long long)save_us, written.bpm,
               read_back.bpm, (unsigned long)failures);
        console_pump();
    }

    /* And the directory still reads, which is the cheapest check that the
     * entry rewrite did not damage anything around it. */
    char name[FAT_NAME_MAX];
    bool is_dir = false;
    uint32_t size = 0, listed = 0;
    for (uint32_t i = 0; i < 16; i++) {
        if (!fat_list("/songs", i, name, sizeof(name), &is_dir, &size)) {
            break;
        }
        printf("RESULT case=save entry=%s bytes=%lu\n", name,
               (unsigned long)size);
        listed++;
    }
    check(listed > 0, "the directory still lists");

    printf("RESULT case=save total_failures=%lu\n", (unsigned long)failures);
    printf("DONE spike=rt-save\n");

    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
