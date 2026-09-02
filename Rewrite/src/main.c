/* Phase 1 bring-up: prove the audio core on hardware before anything else.
 *
 * No card, no display, no keys yet - a synthesised sample, triggered on a
 * timer, so the only thing under test is the mixer and the output path. If this
 * cannot run for ten minutes without an underrun, nothing built on top of it
 * matters.
 */

#include <stdio.h>

#include "hardware/clocks.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "console.h"
#include "fat.h"
#include "kit.h"
#include "sd.h"

/* -30 dBFS at the sample, and the master starts at -12 dB, so the badge makes a
 * tick rather than a bang. This drives a real speaker through an amp with no
 * volume control of its own: the sample value IS the volume. */
#define TEST_PEAK 1024
#define TEST_MASTER 0x2000

/* A short decaying blip - 80 ms, which is drum-shaped and long enough that two
 * voices genuinely overlap when a retrigger lands. */
#define TEST_MS 80

static const int16_t *make_blip(uint32_t *frames_out) {
    uint32_t frames = (SAMPLE_RATE * TEST_MS) / 1000;
    int16_t *data = audio_arena_alloc(frames);
    if (data == NULL) {
        *frames_out = 0;
        return NULL;
    }
    /* A falling square-ish tone. Integer only: there is no reason to pull in
     * floating point for a test signal, and none of this is in the audio path
     * anyway. */
    for (uint32_t i = 0; i < frames; i++) {
        int32_t envelope = (int32_t)(frames - i) * TEST_PEAK / (int32_t)frames;
        int32_t phase = (i * 220 * 2) / SAMPLE_RATE; /* ~220 Hz */
        data[i] = (int16_t)((phase & 1) ? envelope : -envelope);
    }
    *frames_out = frames;
    return data;
}

int main(void) {
    console_begin("rt-phase1");

    audio_init();

    uint32_t frames = 0;
    const int16_t *blip = make_blip(&frames);
    printf("RESULT case=init sys_clk_hz=%lu block_frames=%d voices=%d "
           "arena_bytes=%d blip_frames=%lu arena_free=%lu\n",
           (unsigned long)clock_get_hz(clk_sys), BLOCK_FRAMES, VOICE_COUNT,
           ARENA_BYTES, (unsigned long)frames,
           (unsigned long)audio_arena_free());

    if (blip == NULL) {
        printf("RESULT case=init FAILED no arena space\n");
        while (true) {
            console_pump();
            sleep_ms(10);
        }
    }

    /* The card, if it is there. The default kit is the one sequencer.py
     * settles on, and the paths are where the CircuitPython firmware keeps
     * them - so this reads the player's actual samples, not a copy. */
    static const char *DEFAULT_KIT[] = {
        "/samples/kick_crater.wav",
        "/samples/snare_kraken-head_1.wav",
        "/samples/hh_hats-closed_1.wav",
        "/samples/hh_hats-open_1.wav",
    };

    bool card = sd_init();
    bool mounted = card && fat_mount();
    printf("RESULT case=storage card=%d mounted=%d blocks=%lu\n", card ? 1 : 0,
           mounted ? 1 : 0, (unsigned long)sd_blocks());

    uint32_t loaded_tracks = 0;
    if (mounted) {
        absolute_time_t load_start = get_absolute_time();
        for (uint8_t t = 0; t < (uint8_t)count_of(DEFAULT_KIT); t++) {
            uint32_t frames_loaded = 0;
            enum kit_result r = kit_load_track(t, DEFAULT_KIT[t], &frames_loaded);
            printf("RESULT case=kit track=%u path=%s result=%s frames=%lu\n", t,
                   DEFAULT_KIT[t], kit_result_name(r),
                   (unsigned long)frames_loaded);
            if (r == KIT_OK) {
                loaded_tracks++;
            }
        }
        printf("RESULT case=kit loaded=%lu arena_used=%lu arena_free=%lu "
               "load_us=%lld\n",
               (unsigned long)loaded_tracks, (unsigned long)audio_arena_used(),
               (unsigned long)audio_arena_free(),
               (long long)absolute_time_diff_us(load_start, get_absolute_time()));
    }

    /* Any track the card did not fill falls back to the synthesised blip, so a
     * missing card is a quieter badge rather than a silent one. */
    for (uint8_t t = 0; t < TRACK_COUNT; t++) {
        if (t >= loaded_tracks) {
            audio_set_sample(t, blip, frames);
        }
        /* An octave below to an octave above across the eight, which exercises
         * the interpolator in both directions rather than only at unity. */
        audio_set_pitch(t, (PITCH_UNITY / 2) + (PITCH_UNITY * t) / (TRACK_COUNT - 1));
        audio_set_gain(t, 0x6000);
    }
    audio_set_master(TEST_MASTER);
    audio_start();

    printf("RESULT case=running note=stream is live and silent\n");

    uint32_t tick = 0;
    absolute_time_t next_trigger = make_timeout_time_ms(500);
    absolute_time_t next_report = make_timeout_time_ms(2000);

    while (true) {
        console_pump();

        if (time_reached(next_trigger)) {
            /* Every track at once, faster than the blip decays, so both voices
             * of all eight tracks overlap. That is 16 voices - the worst case
             * the instrument can produce - rather than the one-at-a-time load
             * that says nothing about the budget. */
            for (uint8_t t = 0; t < TRACK_COUNT; t++) {
                audio_trigger(t);
            }
            tick++;
            next_trigger = make_timeout_time_ms(40);
        }

        if (time_reached(next_report)) {
            printf("RESULT case=stats blocks=%lu underruns=%lu worst_cycles=%lu "
                   "active=%lu peak=%lu triggers=%lu\n",
                   (unsigned long)audio_blocks(),
                   (unsigned long)audio_underruns(),
                   (unsigned long)audio_worst_cycles(),
                   (unsigned long)audio_active_voices(),
                   (unsigned long)audio_peak_voices(),
                   (unsigned long)tick);
            next_report = make_timeout_time_ms(2000);
        }

        sleep_ms(1);
    }
}
