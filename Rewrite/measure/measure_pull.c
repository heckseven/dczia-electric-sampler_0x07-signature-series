/* Phase 3, the last criterion: pull the power mid-save.
 *
 * The write ordering claims that a save leaves either the old song or the new
 * one and never neither. Every other check in this phase tested what happens
 * when nothing goes wrong. This tests the thing the ordering was designed for,
 * and it cannot be done without somebody pulling a cable.
 *
 * Two songs, A and B, alike in nothing: different tempo, division, lengths,
 * mutes, volumes and steps. The badge writes them alternately as fast as the
 * card allows. Whenever power returns, the first thing it does is read the file
 * back and say which of the two it is - or that it is neither, which is the
 * failure.
 *
 * It does NOT resume saving on its own. A firmware that started writing again
 * at boot would overwrite the evidence of what the last pull produced, which is
 * the whole measurement. It waits for the host to ask.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/watchdog.h"
#include "pico/stdlib.h"

#include "console.h"
#include "fat.h"
#include "sd.h"
#include "song.h"
#include "songfile.h"

#define PULL_PATH "/songs/cpull.song"

static struct song a, b, found;

static void make_a(struct song *s) {
    song_init(s);
    song_set_bpm(s, 100);
    song_set_division(s, DIVISION_SIXTEENTH);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(s, (uint8_t)t, 4);
        s->muted[t] = 0;
        s->volume_q12[t] = 4096;
        for (uint32_t step = 0; step < 4; step++) {
            song_set_step(s, (uint8_t)t, step, 11, 0);
        }
    }
}

static void make_b(struct song *s) {
    song_init(s);
    song_set_bpm(s, 200);
    song_set_division(s, DIVISION_EIGHTH);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(s, (uint8_t)t, 16);
        s->muted[t] = 1;
        s->volume_q12[t] = 2048;
        for (uint32_t step = 0; step < 16; step++) {
            song_set_step(s, (uint8_t)t, step, 99, 0);
        }
    }
}

static bool same(const struct song *x, const struct song *y) {
    if (x->bpm != y->bpm || x->division != y->division) {
        return false;
    }
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        if (x->lengths[t] != y->lengths[t] || x->muted[t] != y->muted[t]) {
            return false;
        }
        if (memcmp(x->steps[t], y->steps[t], MAX_STEPS) != 0) {
            return false;
        }
        if (memcmp(x->offsets[t], y->offsets[t], MAX_STEPS) != 0) {
            return false;
        }
    }
    return true;
}

/* The same small fsck the reset test uses: every chain terminates where the
 * directory says, and no file starts on a cluster another already holds. A
 * torn write that damaged the filesystem shows up here rather than in the
 * song. */
static uint32_t audit(const char *directory, uint32_t *files_out) {
    static uint32_t seen[128];
    uint32_t seen_count = 0, problems = 0;
    char name[FAT_NAME_MAX];
    bool is_dir = false;
    uint32_t size = 0;

    for (uint32_t i = 0; i < 64; i++) {
        if (!fat_list(directory, i, name, sizeof(name), &is_dir, &size)) {
            break;
        }
        if (is_dir) {
            continue;
        }
        char path[FAT_NAME_MAX + 24];
        strcpy(path, directory);
        strcat(path, "/");
        strncat(path, name, FAT_NAME_MAX - 1);

        struct fat_file file;
        if (!fat_open(path, &file)) {
            problems++;
            continue;
        }
        uint32_t total = 0, got;
        uint8_t chunk[64];
        while ((got = fat_read(&file, chunk, sizeof(chunk))) != 0) {
            total += got;
        }
        if (total != size) {
            problems++;
        }
        if (file.first_cluster >= 2 && seen_count < count_of(seen)) {
            for (uint32_t j = 0; j < seen_count; j++) {
                if (seen[j] == file.first_cluster) {
                    problems++;
                }
            }
            seen[seen_count++] = file.first_cluster;
        }
    }
    *files_out = seen_count;
    return problems;
}

int main(void) {
    console_begin("rt-pull");

    make_a(&a);
    make_b(&b);

    bool mounted = sd_init() && fat_mount();
    printf("RESULT case=pull mounted=%d\n", mounted ? 1 : 0);
    if (!mounted) {
        printf("DONE spike=rt-pull\n");
        while (true) {
            console_pump();
            sleep_ms(10);
        }
    }

    /* The verdict on whatever the last run left behind. */
    enum songfile_result got = songfile_load(PULL_PATH, &found);
    const char *verdict;
    if (got != SONGFILE_OK) {
        verdict = (got == SONGFILE_NO_FILE) ? "ABSENT" : "UNREADABLE";
    } else if (same(&found, &a)) {
        verdict = "A";
    } else if (same(&found, &b)) {
        verdict = "B";
    } else {
        verdict = "TORN";
    }

    uint32_t songs_files = 0, samples_files = 0;
    uint32_t problems = audit("/songs", &songs_files);
    problems += audit("/samples", &samples_files);

    printf("RESULT case=pull verdict=%s load=%s bpm=%u files=%lu "
           "fs_problems=%lu\n",
           verdict, songfile_result_name(got), found.bpm,
           (unsigned long)(songs_files + samples_files),
           (unsigned long)problems);

    /* Wait to be asked, and keep saying so.
     *
     * Resuming on its own would overwrite the very thing the previous pull
     * produced. Saying it once was not enough either: the host needs a second
     * or two to notice the port after power returns, and a verdict printed
     * before it connected is a verdict nobody reads - which is exactly what
     * happened to the first five pulls. */
    bool self_reset = false;
    uint32_t announced = 0;
    for (;;) {
        console_pump();
        int c = getchar_timeout_us(1000);
        if (c == 'S' || c == 's') {
            self_reset = false;
            break;
        }
        if (c == 'R' || c == 'r') {
            /* Self-pull: write, then reset without warning.
             *
             * Not a substitute for cutting power - the card keeps its supply,
             * so it cannot show a torn write. It exists to exercise the host
             * runner's disconnect-and-reconnect path, which got two pulls'
             * worth of a person's time wrong before anyone noticed. Validating
             * the harness on the machine's time rather than theirs. */
            self_reset = true;
            break;
        }
        if ((announced++ % 400) == 0) {
            printf("RESULT case=pull verdict=%s files=%lu fs_problems=%lu "
                   "ready - send S to start writing\n",
                   verdict, (unsigned long)(songs_files + samples_files),
                   (unsigned long)problems);
        }
    }

    printf("RESULT case=pull writing - pull power at any time\n");
    uint32_t round = 0;
    for (;;) {
        songfile_save(PULL_PATH, (round & 1) ? &b : &a);
        round++;
        if ((round % 20) == 0) {
            printf("RESULT case=pull saves=%lu\n", (unsigned long)round);
        }
        console_pump();
        if (self_reset && round >= 40) {
            printf("RESULT case=pull self-resetting after %lu saves\n",
                   (unsigned long)round);
            sleep_ms(50);
            watchdog_reboot(0, 0, 0);
            while (true) {
                tight_loop_contents();
            }
        }
    }
}
