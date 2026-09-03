/* Phase 3: does saving a thousand times cost anything, and can it be done while
 * the badge is playing?
 *
 * Both at once, because separately they each miss something. A save loop with
 * no audio would not notice that the card holds the SPI bus for milliseconds
 * at a time. Audio with one save would not notice a slow leak. Together they
 * ask the question a player would: can I keep working while it plays, and does
 * the card fill up behind my back.
 *
 * The leak question is real rather than theoretical. The write path frees the
 * old chain only after the new directory entry is committed, and deliberately
 * leaks rather than risking corruption if anything fails in between - so
 * "clusters free before" against "clusters free after" is the measurement that
 * says whether that policy costs anything in practice.
 */

#include <stdio.h>

#include "pico/stdlib.h"

#include "audio.h"
#include "console.h"
#include "fat.h"
#include "sd.h"
#include "seq.h"
#include "song.h"
#include "songfile.h"

#define ROUNDS 1000
#define TEST_PATH "/songs/csoak.song"

/* Quiet: the same cap the rest of the campaign uses. */
#define TEST_PEAK 256

static struct song song;
static struct song reloaded;
static struct seq seq;

/* Run the scheduler while the card is busy.
 *
 * A save is tens of milliseconds during which the main loop is inside sd_write,
 * and the sequencer books hits only a short way ahead - so without this it
 * simply stops booking them and they arrive late. The audio never notices,
 * because it is on the other core with its own buffers, which is why the first
 * run of this soak reported zero underruns and hundreds of late hits: two
 * different failures that look nothing alike. */
static void keep_time(void) {
    seq_update(&seq);
}

int main(void) {
    console_begin("rt-soak");

    audio_init();

    /* A short blip, so the sequencer has something to play while the card is
     * busy. What it sounds like does not matter; that it keeps sounding does. */
    uint32_t frames = SAMPLE_RATE / 20;
    int16_t *blip = audio_arena_alloc(frames);
    for (uint32_t i = 0; i < frames; i++) {
        int32_t envelope = (int32_t)(frames - i) * TEST_PEAK / (int32_t)frames;
        blip[i] = (int16_t)(((i * 220 * 2) / SAMPLE_RATE) & 1 ? envelope
                                                              : -envelope);
    }

    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        audio_set_sample((uint8_t)t, blip, frames);
        audio_set_gain((uint8_t)t, 0x4000);
        song_set_length(&song, (uint8_t)t, 8);
    }
    for (uint32_t s = 0; s < 8; s += 2) {
        song_set_step(&song, 0, s, VELOCITY_DEFAULT, 0);
    }
    for (uint32_t s = 1; s < 8; s += 2) {
        song_set_step(&song, 2, s, 80, 0);
    }
    audio_set_master(0x2000);
    seq_init(&seq, &song);
    audio_start();

    sd_set_idle_hook(keep_time);
    bool mounted = sd_init() && fat_mount();
    printf("RESULT case=soak mounted=%d cluster_bytes=%lu\n", mounted ? 1 : 0,
           (unsigned long)fat_cluster_bytes());
    if (!mounted) {
        printf("DONE spike=rt-soak\n");
        while (true) {
            console_pump();
            sleep_ms(10);
        }
    }

    /* Start from a known file, so the count below measures overwriting rather
     * than the one-off cost of creating. */
    (void)fat_delete(TEST_PATH);
    songfile_save(TEST_PATH, &song);

    absolute_time_t counting = get_absolute_time();
    uint32_t free_before = fat_count_free();
    int64_t count_us = absolute_time_diff_us(counting, get_absolute_time());
    printf("RESULT case=soak free_before=%lu count_us=%lld\n",
           (unsigned long)free_before, (long long)count_us);

    seq_start(&seq);

    uint32_t save_fail = 0, load_fail = 0, mismatch = 0;
    uint32_t worst_save_us = 0;
    uint64_t total_save_us = 0;

    for (uint32_t round = 0; round < ROUNDS; round++) {
        /* Change something every round, so a save that quietly did nothing
         * would show up as a mismatch rather than passing. */
        song_set_bpm(&song, (int32_t)(60 + (round % 200)));
        song_set_step(&song, 1, round % 8, (uint8_t)(1 + (round % 126)), 0);

        absolute_time_t started = get_absolute_time();
        if (songfile_save(TEST_PATH, &song) != SONGFILE_OK) {
            save_fail++;
        }
        uint32_t us = (uint32_t)absolute_time_diff_us(started,
                                                      get_absolute_time());
        total_save_us += us;
        if (us > worst_save_us) {
            worst_save_us = us;
        }

        if (songfile_load(TEST_PATH, &reloaded) != SONGFILE_OK) {
            load_fail++;
        } else if (reloaded.bpm != song.bpm ||
                   reloaded.steps[1][round % 8] != song.steps[1][round % 8]) {
            mismatch++;
        }

        seq_update(&seq);
        console_pump();

        if ((round % 100) == 0) {
            printf("RESULT case=soak round=%lu underruns=%lu late=%lu "
                   "seqhits=%lu\n",
                   (unsigned long)round, (unsigned long)audio_underruns(),
                   (unsigned long)audio_late(), (unsigned long)seq.hits);
        }
    }

    seq_stop(&seq);
    uint32_t free_after = fat_count_free();

    printf("RESULT case=soak rounds=%d save_fail=%lu load_fail=%lu "
           "mismatch=%lu mean_save_us=%lu worst_save_us=%lu\n",
           ROUNDS, (unsigned long)save_fail, (unsigned long)load_fail,
           (unsigned long)mismatch, (unsigned long)(total_save_us / ROUNDS),
           (unsigned long)worst_save_us);
    printf("RESULT case=soak free_before=%lu free_after=%lu leaked=%ld "
           "underruns=%lu late=%lu seqhits=%lu\n",
           (unsigned long)free_before, (unsigned long)free_after,
           (long)((int32_t)free_before - (int32_t)free_after),
           (unsigned long)audio_underruns(), (unsigned long)audio_late(),
           (unsigned long)seq.hits);
    printf("DONE spike=rt-soak\n");

    audio_stop_all();
    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
