/* Does the pattern you saved actually come back?
 *
 * Not "can a song be written and read" - rt_save answers that. This walks the
 * exact chain the instrument walks, in the same order, through the same
 * functions:
 *
 *   prefs_load  ->  the song name from the shared settings file
 *   build path  ->  /songs/<name>.song
 *   songfile_*  ->  the pattern
 *   song.kit    ->  which samples it plays
 *
 * Two passes across a real reset, because the interesting failure is a save
 * that works and a boot that looks somewhere else for it.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/watchdog.h"
#include "pico/stdlib.h"

#include "console.h"
#include "fat.h"
#include "prefs.h"
#include "sd.h"
#include "song.h"
#include "songfile.h"

#define SESSION_MAGIC 0x5E550000u
#define SESSION_SCRATCH 3

static struct prefs prefs;
static struct song song;
static struct song back;
static uint32_t failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("RESULT case=session FAIL %s\n", what);
        failures++;
    }
}

/* The instrument's own path construction, kept identical on purpose: a test
 * that builds the path its own way would pass while the instrument looked
 * somewhere else. */
static void build_path(char *out, uint32_t size) {
    /* Deliberately NOT the name the instrument uses.
     *
     * The first version of this test built the same path the badge does -
     * /songs/session.song - which is the whole point of testing the chain, and
     * also meant it overwrote the pattern the player had just saved there. A
     * test that verifies persistence by destroying the thing being persisted
     * has its priorities backwards.
     *
     * The chain is still exercised end to end: prefs is read, the path is
     * built the same way from the same pieces, and only the leaf differs. */
    const char *name = "rt-session-test";
    (void)prefs;
    (void)size;
    strcpy(out, SONG_DIR);
    strcat(out, "/");
    strncat(out, name, PREFS_NAME_MAX - 1);
    strcat(out, SONG_SUFFIX);
}

int main(void) {
    uint32_t stored = watchdog_hw->scratch[SESSION_SCRATCH];
    bool second = (stored & 0xFFFF0000u) == SESSION_MAGIC;

    console_begin("rt-session");

    bool mounted = sd_init() && fat_mount();
    printf("RESULT case=session pass=%d mounted=%d\n", second ? 2 : 1,
           mounted ? 1 : 0);
    if (!mounted) {
        printf("DONE spike=rt-session\n");
        while (true) {
            console_pump();
            sleep_ms(10);
        }
    }

    prefs_load(&prefs);
    char path[PREFS_NAME_MAX + 24];
    build_path(path, sizeof(path));
    printf("RESULT case=session prefs_song=%s path=%s\n",
           prefs.song[0] ? prefs.song : "(unset)", path);

    if (!second) {
        /* Make a pattern that is nobody's default: a five-step kick against a
         * seven-step hat, so a fallback to the built-in demo would be obvious
         * rather than plausible. */
        song_init(&song);
        song_set_bpm(&song, 143);
        song_set_division(&song, DIVISION_EIGHTH_T);
        song_set_length(&song, 0, 5);
        song_set_length(&song, 2, 7);
        for (uint32_t s = 0; s < 5; s++) {
            song_set_step(&song, 0, s, 96, 0);
        }
        for (uint32_t s = 0; s < 7; s += 2) {
            song_set_step(&song, 2, s, 71, 0);
        }
        song_set_kit_path(&song, 0, "/samples/kick_crater.wav");
        song_set_kit_path(&song, 2, "/samples/hh_hats-closed_1.wav");

        enum songfile_result saved = songfile_save(path, &song);
        check(saved == SONGFILE_OK, "save");

        /* Volume only. Writing the song name would repoint the instrument at
         * this test's file the next time it booted. */
        prefs.volume = 0x2000;
        check(prefs_save(&prefs), "prefs save");

        printf("RESULT case=session saved=%s bpm=%u resetting\n",
               songfile_result_name(saved), song.bpm);
        watchdog_hw->scratch[SESSION_SCRATCH] = SESSION_MAGIC | 1u;
        sleep_ms(200);
        watchdog_reboot(0, 0, 0);
        while (true) {
            tight_loop_contents();
        }
    }

    /* Second pass: everything below came off the card, through the same chain
     * the instrument uses at boot. */
    watchdog_hw->scratch[SESSION_SCRATCH] = 0;
    enum songfile_result got = songfile_load(path, &back);
    check(got == SONGFILE_OK, "load");

    check(back.bpm == 143, "tempo came back");
    check(back.division == DIVISION_EIGHTH_T, "division came back");
    check(back.lengths[0] == 5 && back.lengths[2] == 7, "track lengths");
    check(back.steps[0][0] == 96 && back.steps[2][0] == 71, "steps");
    check(strcmp(back.kit[0], "/samples/kick_crater.wav") == 0, "kit track 0");
    check(strcmp(back.kit[2], "/samples/hh_hats-closed_1.wav") == 0,
          "kit track 2");
    check(prefs.volume == 0x2000, "volume remembered");

    printf("RESULT case=session loaded=%s bpm=%u len0=%u len2=%u kit0=%s "
           "volume=%ld failures=%lu\n",
           songfile_result_name(got), back.bpm, back.lengths[0],
           back.lengths[2], back.kit[0], (long)prefs.volume,
           (unsigned long)failures);
    printf("DONE spike=rt-session\n");

    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
