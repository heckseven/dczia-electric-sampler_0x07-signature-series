/* The .song format, round-tripped without a card.
 *
 * This file exists because every bug this format has had was found on hardware:
 * a float encoder that doubled its input, a reader thrown out of step by a nil
 * it did not expect, keys arriving in the Python's hash order rather than the
 * order they were written, and two settings dropped on write so that saving
 * from the badge quietly reset them. All four are host-testable and none of
 * them needed a badge, a card, or a power pull to find.
 */

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "fat.h"
#include "song.h"
#include "songfile.h"

/* --- the filesystem, stubbed ---------------------------------------------- */

static uint8_t written[8192];
static uint32_t written_length;

bool fat_open(const char *path, struct fat_file *file) {
    (void)path;
    (void)file;
    return false;
}
uint32_t fat_read(struct fat_file *file, void *buffer, uint32_t length) {
    (void)file;
    (void)buffer;
    (void)length;
    return 0;
}
bool fat_write(const char *path, const uint8_t *data, uint32_t length) {
    (void)path;
    if (length > sizeof(written)) {
        return false;
    }
    memcpy(written, data, length);
    written_length = length;
    return true;
}

/* --- harness --------------------------------------------------------------- */

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

/* A song with something distinctive in every field, so a dropped one shows up
 * as a difference rather than matching the default by luck. */
static void fill(struct song *song) {
    song_init(song);
    song_set_bpm(song, 137);
    song_set_division(song, DIVISION_EIGHTH);
    for (uint8_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(song, t, 3u + t);
        song->muted[t] = (t % 3 == 0);
        /* Values chosen to survive the trip through thousandths exactly, so a
         * failure here means a lost field and not a rounding step. */
        static const uint16_t levels[] = {4096, 2048, 8192, 1024,
                                          0,    4096, 2048, 1024};
        song->volume_q12[t] = levels[t];
        song->kit_volume_q12[t] = levels[(t + 3) % 8];
        song->track_strength[t] = (t == 2) ? -1 : (int8_t)(t * 2);
    }
    song_set_kit_path(song, 0, "/sd/samples/kick_crater.wav");
    song_set_kit_path(song, 5, "/sd/samples/hh_hats-closed_1.wav");
    for (uint32_t s = 0; s < MAX_STEPS; s += 5) {
        song_set_step(song, 1, s, (uint8_t)(1 + (s % 127)), (s % 3) - 1);
    }
}

static void test_round_trip_keeps_everything(void) {
    struct song out;
    fill(&out);

    uint8_t buffer[8192];
    uint32_t length = 0;
    check(songfile_encode(buffer, sizeof(buffer), &out, &length) == SONGFILE_OK,
          "the song encodes");

    struct song back;
    check(songfile_decode(buffer, length, &back) == SONGFILE_OK,
          "and decodes again");

    check(back.bpm == out.bpm, "bpm survives");
    check(back.division == out.division, "division survives");
    for (uint8_t t = 0; t < TRACK_COUNT; t++) {
        check(back.lengths[t] == out.lengths[t], "lengths survive");
        check(back.muted[t] == out.muted[t], "mutes survive");
        check(back.volume_q12[t] == out.volume_q12[t], "track levels survive");
        /* The two that a previous writer dropped on the floor. */
        check(back.kit_volume_q12[t] == out.kit_volume_q12[t],
              "kit levels survive");
        check(back.track_strength[t] == out.track_strength[t],
              "per-track quantise survives");
    }
    check(strcmp(back.kit[0], out.kit[0]) == 0, "a kit path survives");
    check(strcmp(back.kit[5], out.kit[5]) == 0, "and one further along");
    check(back.kit[3][0] == '\0', "a track with no sample stays empty");
    check(memcmp(back.steps, out.steps, sizeof(out.steps)) == 0,
          "every step survives");
    check(memcmp(back.offsets, out.offsets, sizeof(out.offsets)) == 0,
          "every offset survives");
}

static void test_kit_paths_keep_the_prefix_the_python_expects(void) {
    /* The Python mounts the card at /sd and this firmware at the root, so the
     * prefix is stripped on the way in and put back on the way out. A song
     * saved here has to open there with its sounds rather than in silence. */
    struct song song;
    song_init(&song);
    song_set_kit_path(&song, 0, "/sd/samples/kick_crater.wav");
    check(strcmp(song.kit[0], "/samples/kick_crater.wav") == 0,
          "the prefix is stripped in memory");

    uint32_t length = 0;
    uint8_t buffer[8192];
    songfile_encode(buffer, sizeof(buffer), &song, &length);
    bool found = false;
    for (uint32_t i = 0; i + 26 <= length; i++) {
        if (memcmp(&buffer[i], "/sd/samples/kick_crater.wav", 27) == 0) {
            found = true;
        }
    }
    check(found, "and put back on the way out");
}

static void test_a_truncated_file_is_refused(void) {
    /* Not "returns whatever it managed to read": a half-decoded song loaded
     * over a good one in memory is worse than a load that failed. */
    struct song song;
    fill(&song);
    uint8_t buffer[8192];
    uint32_t length = 0;
    songfile_encode(buffer, sizeof(buffer), &song, &length);

    struct song back;
    check(songfile_decode(buffer, length / 2, &back) == SONGFILE_NOT_A_SONG,
          "half a song is not a song");
    check(songfile_decode(buffer, 0, &back) == SONGFILE_NOT_A_SONG,
          "and neither is none of one");
}

static void test_unknown_keys_are_stepped_over(void) {
    /* A newer Python writing a key this firmware does not know must not
     * desynchronise the rest of the map - which is exactly what an unexpected
     * nil did once, and it took the whole file with it. */
    static const uint8_t doc[] = {
        0x83,                                     /* map of 3 */
        0xA3, 'b',  'p',  'm',  0xCD, 0x00, 0x96, /* bpm: 150 */
        0xA7, 'm',  'y',  's',  't',  'e',  'r', 'y',
        0x93, 0xC0, 0x01, 0xCA, 0x40, 0x00, 0x00, 0x00, /* [nil, 1, 2.0] */
        0xA8, 'd',  'i',  'v',  'i',  's',  'i', 'o', 'n',
        0x01, /* division: 1 */
    };
    struct song song;
    check(songfile_decode(doc, sizeof(doc), &song) == SONGFILE_OK,
          "an unknown key does not fail the load");
    check(song.bpm == 150, "the key before it read correctly");
    check(song.division == 1, "and so did the key after it");
}

int main(void) {
    test_round_trip_keeps_everything();
    test_kit_paths_keep_the_prefix_the_python_expects();
    test_a_truncated_file_is_refused();
    test_unknown_keys_are_stepped_over();

    if (failures == 0) {
        printf("ok - all song file tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
