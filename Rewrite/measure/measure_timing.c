/* Phase 2 criterion 2: is a sequenced step actually sample-accurate?
 *
 * The sequencer computes step boundaries in frames, so checking its schedule
 * against its own arithmetic proves nothing - it would agree with itself. What
 * matters is whether the sound leaves the pin when the schedule says it should.
 *
 * So: run the transport, and for every hit compare the moment the data pin
 * first goes high against the frame the sequencer scheduled it for. The
 * difference carries a constant - the TX FIFO, the PIO serialisation, the bit
 * position of the first set bit, all of which Task 5 measured as 568 us - and
 * that constant is not what is being measured. The **spread** is. A perfectly
 * timed sequencer has a spread of one frame; the current firmware's would be
 * tens of milliseconds, because its clock is the main loop.
 */

#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/pio.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "console.h"
#include "seq.h"
#include "song.h"
#include "watch.pio.h"

#define HITS 120

/* Deliberately awkward combinations.
 *
 * 240 BPM at 1/16 is exactly 1,000 frames a step, so the fractional part of the
 * tick accumulator never moves and a broken one would still pass. The rest do
 * not divide evenly: at 127 BPM a 1/16 step is 1,889.76 frames, so the fraction
 * has to carry correctly or the error walks. */
struct trial {
    uint16_t bpm;
    uint8_t division;
};

static const struct trial TRIALS[] = {
    {240, DIVISION_SIXTEENTH},    /* exact: 1,000 frames a step */
    {127, DIVISION_SIXTEENTH},    /* 1,889.76 - the fraction must carry */
    {143, DIVISION_EIGHTH_T},     /* triplets, and an odd tempo */
    {97, DIVISION_THIRTYSECOND},  /* the shortest step */
    {300, DIVISION_QUARTER},      /* the ends of the tempo range */
    {20, DIVISION_QUARTER},
};
#define TEST_VALUE 0x0100

static PIO pio = pio0;
static struct song song;
static struct seq seq;

int main(void) {
    console_begin("rt-timing");
    audio_init();

    /* A short blip so consecutive steps do not overlap, and one that starts at
     * full value so the watcher sees it on the first frame. */
    /* Short: 64 frames is 4 ms against a 62.5 ms step at 240 BPM 1/16. The gap
     * matters because the watcher re-arms the instant the pin goes high again -
     * a tone still sounding when the IRQ is cleared fires it immediately, and
     * the next hit then measures a stale edge. */
    uint32_t frames = 64;
    int16_t *tone = audio_arena_alloc(frames);
    for (uint32_t i = 0; i < frames; i++) {
        tone[i] = TEST_VALUE;
    }
    audio_set_sample(0, tone, frames);
    audio_set_pitch(0, PITCH_UNITY);
    audio_set_gain(0, 0x7FFF);
    audio_set_master(0x7FFF);

    song_init(&song);
    song_set_length(&song, 0, 1);
    song_set_step(&song, 0, 0, VELOCITY_MAX, 0);
    for (uint32_t t = 1; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 1);
    }
    seq_init(&seq, &song);

    uint offset = pio_add_program(pio, &data_watch_program);
    uint sm_watch = pio_claim_unused_sm(pio, true);
    float div = (float)clock_get_hz(clk_sys) /
                (float)(SAMPLE_RATE * I2S_BITS_PER_FRAME);
    data_watch_init(pio, sm_watch, offset, PIN_I2S_DATA, div);
    pio_sm_set_enabled(pio, sm_watch, true);

    audio_start();
    busy_wait_us(50000);

    for (uint32_t trial = 0; trial < count_of(TRIALS); trial++) {
        song_set_bpm(&song, TRIALS[trial].bpm);
        song_set_division(&song, TRIALS[trial].division);

        /* The step in frames, reported so a reader can see which trials are the
         * awkward ones: a whole number means the fraction sat idle. */
        uint64_t numerator =
            (uint64_t)SAMPLE_RATE * 60u * song_ticks_per_step(&song) * 1000u;
        uint64_t thousandths = numerator / ((uint64_t)song.bpm * PPQN);

        seq_start(&seq);
        uint64_t base_frame = seq.start_frame;
        uint32_t base_us = timer_hw->timerawl;

        int32_t best = 0x7FFFFFFF, worst = -0x7FFFFFFF;
        /* Which hit was the outlier. A single stray reading hiding inside
         * min/mean/max looks identical to broad jitter and wants a completely
         * different fix; knowing it is always hit zero - or never is - settles
         * that in one number. */
        uint32_t best_index = 0;
        uint32_t discarded = 0;
        int32_t warmup_us = 0;
        int64_t total = 0;
        uint32_t counted = 0, missed = 0, last_hits = seq.hits;

        while (counted < HITS) {
            seq_update(&seq);
            console_pump();
            if (seq.hits == last_hits) {
                continue;
            }
            last_hits = seq.hits;
            uint64_t scheduled = seq.last_hit_frame;

            /* Clear BEFORE the hit sounds. The tail of the previous tone would
             * otherwise re-raise it and the next hit would measure an edge that
             * was not its own - worth 6.5 ms of invented spread. */
            pio_interrupt_clear(pio, 0);

            uint32_t guard = 0;
            while (!pio_interrupt_get(pio, 0)) {
                if (++guard > 20000000u) {
                    break;
                }
            }
            uint32_t fired_us = timer_hw->timerawl;
            if (guard > 20000000u) {
                missed++;
                continue;
            }

            int32_t measured_us = (int32_t)(fired_us - base_us);
            int32_t expected_us =
                (int32_t)(((int64_t)scheduled - (int64_t)base_frame) * 1000000 /
                          SAMPLE_RATE);
            int32_t error = measured_us - expected_us;

            /* Discard the first hit of each trial, and say so rather than
             * quietly dropping it.
             *
             * It reads about 30 us where every other hit in the same trial
             * reads 20,000, and with the transport now starting a full
             * lookahead in the future that is not something the firmware can
             * do - a hit booked 16 ms out cannot sound in 30 us. It is a stale
             * edge: the previous trial's final tone leaves the watcher armed,
             * and neither a settle nor a read-back-confirmed clear removes it.
             *
             * The first trial, which has no previous tone, shows no such hit
             * (min_at=1, spread 1 us), which is what identifies it. */
            if (discarded == 0) {
                discarded = 1;
                warmup_us = error;
                continue;
            }

            /* The first few, individually. A single outlier hiding inside
             * min/mean/max is indistinguishable from broad jitter, and the two
             * want completely different fixes. */
            if (counted < 4) {
                printf("RESULT case=sample bpm=%u n=%lu error_us=%ld "
                       "lead_frames=%ld\n",
                       song.bpm, (unsigned long)counted, (long)error,
                       (long)((int64_t)scheduled - (int64_t)audio_frames()));
            }

            if (error < best) {
                best = error;
                best_index = counted;
            }
            if (error > worst) {
                worst = error;
            }
            total += error;
            counted++;
        }
        seq_stop(&seq);

        printf("RESULT case=timing bpm=%u division=%s step_frames=%lu.%03lu "
               "hits=%lu missed=%lu min_us=%ld mean_us=%ld max_us=%ld "
               "spread_us=%ld min_at=%lu warmup_us=%ld late=%lu underruns=%lu\n",
               song.bpm, song_division_name(&song),
               (unsigned long)(thousandths / 1000u),
               (unsigned long)(thousandths % 1000u),
               (unsigned long)counted, (unsigned long)missed, (long)best,
               (long)(counted ? total / counted : 0), (long)worst,
               (long)(worst - best), (unsigned long)best_index, (long)warmup_us,
               (unsigned long)audio_late(),
               (unsigned long)audio_underruns());
    }

    printf("DONE spike=rt-timing\n");

    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
