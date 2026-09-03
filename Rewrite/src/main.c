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
#include "display.h"
#include "fat.h"
#include "input.h"
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

    display_init();
    input_init();
    printf("RESULT case=input note=pads 0-7 play, Function+pad selects, "
           "Select turns pitch, Volume turns level\n");

    uint8_t selected = 0;
    uint32_t hits = 0;
    /* Pitch as a 16.16 rate, tracked per track so the knob is relative to
     * whatever the track is already doing rather than to unity. */
    static uint32_t pitch[TRACK_COUNT];
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        pitch[t] = PITCH_UNITY;
        audio_set_pitch((uint8_t)t, PITCH_UNITY);
    }
    int16_t master = TEST_MASTER;
    audio_set_master(master);

    absolute_time_t next_report = make_timeout_time_ms(2000);
    absolute_time_t next_frame = make_timeout_time_ms(33);
    uint32_t pages_written = 0;

    while (true) {
        console_pump();
        input_poll();

        struct input_event event;
        while (input_next(&event)) {
            switch (event.kind) {
            case INPUT_KEY_DOWN:
                if (event.key <= KEY_PAD_LAST) {
                    if (input_held(KEY_FUNCTION)) {
                        selected = event.key;
                    } else {
                        audio_trigger(event.key);
                        hits++;
                    }
                } else if (event.key == KEY_PLAY) {
                    audio_stop_all();
                }
                break;

            case INPUT_SELECT_TURN: {
                /* A semitone a click, as a ratio rather than a table: 2^(1/12)
                 * is 1.0595, and 1090/1029 is that to five decimal places in
                 * integers a Cortex-M0+ can multiply without help. */
                int32_t steps = event.delta;
                uint32_t p = pitch[selected];
                while (steps > 0 && p < PITCH_UNITY * 4) {
                    p = (uint32_t)(((uint64_t)p * 1090u) / 1029u);
                    steps--;
                }
                while (steps < 0 && p > PITCH_UNITY / 4) {
                    p = (uint32_t)(((uint64_t)p * 1029u) / 1090u);
                    steps++;
                }
                pitch[selected] = p;
                audio_set_pitch(selected, p);
                printf("RESULT case=pitch track=%u rate_q16=%lu "
                       "percent=%lu\n",
                       selected, (unsigned long)p,
                       (unsigned long)((uint64_t)p * 100u / PITCH_UNITY));
                break;
            }

            case INPUT_VOLUME_TURN: {
                int32_t next = master + event.delta * 0x0400;
                if (next > 0x7FFF) {
                    next = 0x7FFF;
                }
                if (next < 0) {
                    next = 0;
                }
                master = (int16_t)next;
                audio_set_master(master);
                printf("RESULT case=volume master=%d\n", master);
                break;
            }

            default:
                break;
            }
        }

        /* 30 Hz is faster than anyone reads and slower than the pads move, and
         * the flush only touches pages that actually changed - so a frame where
         * nothing happened costs nothing on the bus. */
        if (time_reached(next_frame)) {
            uint32_t sounding = audio_active_mask();
            for (uint32_t t = 0; t < TRACK_COUNT; t++) {
                uint32_t x = t * 16;
                bool lit = (sounding >> t) & 1u;
                /* Filled while the track is sounding, outlined when idle, and
                 * the selected one keeps a gap down its middle so it reads as
                 * chosen whether or not it happens to be playing. */
                display_fill_rect(x + 1, 1, 14, 14, lit);
                if (!lit) {
                    display_rect(x + 1, 1, 14, 14, true);
                }
                if (t == selected) {
                    display_fill_rect(x + 7, 5, 2, 6, !lit);
                }
            }

            /* Pitch of the selected track, as a bar centred on unity: left of
             * centre is flat, right is sharp, +/- two octaves end to end. */
            uint32_t p = pitch[selected];
            uint32_t span = 0;
            if (p >= PITCH_UNITY) {
                span = 64 + (uint32_t)(((uint64_t)(p - PITCH_UNITY) * 63u) /
                                       (PITCH_UNITY * 3u));
            } else {
                span = 64 - (uint32_t)(((uint64_t)(PITCH_UNITY - p) * 63u) /
                                       (PITCH_UNITY * 3u / 4u));
            }
            if (span > 127) {
                span = 127;
            }
            display_fill_rect(0, 17, OLED_WIDTH, 5, false);
            display_pixel(64, 16, true);
            uint32_t from = span < 64 ? span : 64;
            uint32_t to = span < 64 ? 64 : span;
            display_fill_rect(from, 18, (to - from) + 1, 3, true);

            /* Master level across the bottom. */
            uint32_t level = ((uint32_t)master * OLED_WIDTH) / 0x8000u;
            display_fill_rect(0, 26, OLED_WIDTH, 5, false);
            display_fill_rect(0, 27, level, 3, true);

            pages_written += display_flush();
            next_frame = make_timeout_time_ms(33);
        }

        if (time_reached(next_report)) {
            printf("RESULT case=stats blocks=%lu underruns=%lu "
                   "worst_cycles=%lu peak=%lu hits=%lu selected=%u "
                   "pages=%lu\n",
                   (unsigned long)audio_blocks(),
                   (unsigned long)audio_underruns(),
                   (unsigned long)audio_worst_cycles(),
                   (unsigned long)audio_peak_voices(),
                   (unsigned long)hits, selected,
                   (unsigned long)pages_written);
            next_report = make_timeout_time_ms(2000);
        }
    }
}
