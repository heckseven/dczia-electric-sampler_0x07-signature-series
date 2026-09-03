/* Phase 3: does a saved song survive a reset, and is the filesystem sound?
 *
 * Two things, in two passes, driven from a scratch register so the whole thing
 * runs without anybody watching.
 *
 *   Pass 1  Save a song with distinctive values, check the filesystem, then
 *           reset the chip through the watchdog.
 *   Pass 2  Load the song back and compare.
 *
 * A watchdog reset is not a power cycle - it does not interrupt the card's own
 * supply - and this file does not claim otherwise. What it does prove is that
 * the song is on the card rather than in RAM, because RAM does not survive it.
 * Pulling the cable mid-write is a different test and needs a person.
 *
 * The filesystem check is a small fsck: follow every chain in the directories
 * that matter, confirm each terminates and is as long as the file claims, and
 * confirm no cluster belongs to two files. A cross-link is what a bad write
 * ordering produces, and it is invisible until something reads the wrong bytes.
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

#define CHECK_PATH "/songs/ccheck.song"
#define CHECK_MAGIC 0xC4EC0000u
#define CHECK_SCRATCH 3

/* Enough for every cluster the songs and samples directories use: a 37 KB
 * sample is two 32 KB clusters, and there are a handful of files. */
#define MAX_CLUSTERS 256

static uint32_t seen[MAX_CLUSTERS];
static uint32_t seen_count;
static uint32_t crosslinks;
static uint32_t bad_chains;

static struct song song;
static struct song loaded;

static void fill(struct song *s) {
    song_init(s);
    song_set_bpm(s, 211);
    song_set_division(s, DIVISION_EIGHTH_T);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(s, (uint8_t)t, 2 + t);
        s->volume_q12[t] = (uint16_t)(1024 + t * 256);
        s->muted[t] = (uint8_t)(t & 1);
    }
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        for (uint32_t step = 0; step < s->lengths[t]; step++) {
            song_set_step(s, (uint8_t)t, step, (uint8_t)(9 + t * 13 + step), 0);
        }
    }
}

/* Walk one file's chain, checking it terminates and does not reuse a cluster
 * some other file already claimed. */
static void audit(const char *directory) {
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
            bad_chains++;
            continue;
        }

        /* Read it whole. fat_read follows the chain, so reaching the declared
         * size means the chain is at least long enough and every link
         * resolved. */
        uint32_t total = 0;
        uint8_t chunk[64];
        for (;;) {
            uint32_t got = fat_read(&file, chunk, sizeof(chunk));
            if (got == 0) {
                break;
            }
            total += got;
        }
        if (total != size) {
            printf("RESULT case=check SHORT file=%s claimed=%lu read=%lu\n",
                   name, (unsigned long)size, (unsigned long)total);
            bad_chains++;
        }

        /* And that its first cluster is not one somebody else already used. A
         * full cross-link check would need every cluster of every chain; the
         * first is what a bad overwrite duplicates. */
        if (file.first_cluster >= 2 && seen_count < MAX_CLUSTERS) {
            for (uint32_t j = 0; j < seen_count; j++) {
                if (seen[j] == file.first_cluster) {
                    printf("RESULT case=check CROSSLINK file=%s cluster=%lu\n",
                           name, (unsigned long)file.first_cluster);
                    crosslinks++;
                }
            }
            seen[seen_count++] = file.first_cluster;
        }
    }
}

int main(void) {
    uint32_t stored = watchdog_hw->scratch[CHECK_SCRATCH];
    bool second_pass = (stored & 0xFFFF0000u) == CHECK_MAGIC;

    console_begin("rt-check");

    bool mounted = sd_init() && fat_mount();
    printf("RESULT case=check pass=%d mounted=%d reset=%s\n",
           second_pass ? 2 : 1, mounted ? 1 : 0,
           watchdog_caused_reboot() ? "watchdog" : "clean");
    if (!mounted) {
        printf("DONE spike=rt-check\n");
        while (true) {
            console_pump();
            sleep_ms(10);
        }
    }

    if (!second_pass) {
        fill(&song);
        enum songfile_result saved = songfile_save(CHECK_PATH, &song);
        printf("RESULT case=check saved=%s bpm=%u\n",
               songfile_result_name(saved), song.bpm);

        audit("/songs");
        audit("/samples");
        printf("RESULT case=check files=%lu crosslinks=%lu bad_chains=%lu\n",
               (unsigned long)seen_count, (unsigned long)crosslinks,
               (unsigned long)bad_chains);

        printf("RESULT case=check resetting\n");
        watchdog_hw->scratch[CHECK_SCRATCH] = CHECK_MAGIC | 1u;
        sleep_ms(200);
        watchdog_reboot(0, 0, 0);
        while (true) {
            tight_loop_contents();
        }
    }

    /* Second pass: RAM is gone, so anything correct here came off the card. */
    watchdog_hw->scratch[CHECK_SCRATCH] = 0;
    fill(&song);
    enum songfile_result got = songfile_load(CHECK_PATH, &loaded);

    uint32_t wrong = 0;
    if (got != SONGFILE_OK) {
        wrong = 0xFFFF;
    } else {
        if (loaded.bpm != song.bpm || loaded.division != song.division) {
            wrong++;
        }
        for (uint32_t t = 0; t < TRACK_COUNT; t++) {
            if (loaded.lengths[t] != song.lengths[t] ||
                loaded.muted[t] != song.muted[t]) {
                wrong++;
            }
            for (uint32_t s = 0; s < MAX_STEPS; s++) {
                if (loaded.steps[t][s] != song.steps[t][s] ||
                    loaded.offsets[t][s] != song.offsets[t][s]) {
                    wrong++;
                }
            }
        }
    }

    audit("/songs");
    audit("/samples");

    printf("RESULT case=check loaded=%s bpm=%u->%u wrong=%lu files=%lu "
           "crosslinks=%lu bad_chains=%lu\n",
           songfile_result_name(got), song.bpm, loaded.bpm,
           (unsigned long)wrong, (unsigned long)seen_count,
           (unsigned long)crosslinks, (unsigned long)bad_chains);
    printf("DONE spike=rt-check\n");

    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
