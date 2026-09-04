/* Phase 1 bring-up: prove the audio core on hardware before anything else.
 *
 * No card, no display, no keys yet - a synthesised sample, triggered on a
 * timer, so the only thing under test is the mixer and the output path. If this
 * cannot run for ten minutes without an underrun, nothing built on top of it
 * matters.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/clocks.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "console.h"
#include "display.h"
#include "font.h"
#include "fat.h"
#include "input.h"
#include "kit.h"
#include "seq.h"
#include "song.h"
#include "prefs.h"
#include "songfile.h"
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

/* The transport, at file scope so the card's idle hook can reach it. */
static struct song song;
static struct seq seq;

static struct prefs prefs;

static void keep_time(void) {
    seq_update(&seq);
}

/* Where the current pattern lives on the card.
 *
 * There is no menu yet and therefore no way to type a name, so the badge keeps
 * one working song and remembers which it was. "session" is the default; if the
 * player has a song open from the Python it is that one, because the name comes
 * out of the settings file both firmwares share. */
static char song_path[PREFS_NAME_MAX + 24];

static void build_song_path(void) {
    const char *name = prefs.song[0] ? prefs.song : "session";
    strcpy(song_path, SONG_DIR);
    strcat(song_path, "/");
    strncat(song_path, name, PREFS_NAME_MAX - 1);
    strcat(song_path, SONG_SUFFIX);
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

    /* Keep the sequencer scheduling while the card is busy - see the note on
     * sd_set_idle_hook. Set before the first card operation, so even the kit
     * load benefits. */
    sd_set_idle_hook(keep_time);

    bool card = sd_init();
    bool mounted = card && fat_mount();
    printf("RESULT case=storage card=%d mounted=%d blocks=%lu\n", card ? 1 : 0,
           mounted ? 1 : 0, (unsigned long)sd_blocks());

    /* The song first, then the sounds it names.
     *
     * This order matters: a song carries the paths of the samples it was saved
     * with, so loading the kit before knowing which song is open means loading
     * the wrong one and reloading it a moment later. Songs should sound the way
     * they did when they were saved, not the way this build happens to start. */
    song_init(&song);
    seq_init(&seq, &song);
    bool loaded_song = false;
    uint32_t loaded_tracks = 0;

    if (mounted) {
        prefs_load(&prefs);
        build_song_path();
        enum songfile_result r = songfile_load(song_path, &song);
        printf("RESULT case=song path=%s result=%s bpm=%u div=%s empty=%d "
               "prefs=%d volume=%ld\n",
               song_path, songfile_result_name(r), song.bpm,
               song_division_name(&song), song_is_empty(&song) ? 1 : 0,
               prefs.loaded ? 1 : 0, (long)prefs.volume);
        if (r == SONGFILE_OK) {
            loaded_song = true;
        } else {
            song_init(&song);
        }

        absolute_time_t load_start = get_absolute_time();
        for (uint8_t t = 0; t < TRACK_COUNT; t++) {
            const char *path = song.kit[t][0] ? song.kit[t] : NULL;
            if (path == NULL && t < (uint8_t)count_of(DEFAULT_KIT)) {
                path = DEFAULT_KIT[t];
            }
            if (path == NULL) {
                continue;
            }
            uint32_t frames_loaded = 0;
            enum kit_result kr = kit_load_track(t, path, &frames_loaded);
            printf("RESULT case=kit track=%u path=%s result=%s frames=%lu\n", t,
                   path, kit_result_name(kr), (unsigned long)frames_loaded);
            if (kr == KIT_OK) {
                loaded_tracks++;
                /* Record what was actually loaded, so saving writes the kit
                 * that is playing rather than the one that was asked for. */
                song_set_kit_path(&song, t, path);
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
        /* Unity. The spread of pitches here during bring-up existed to
         * exercise the interpolator in both directions; a player wants their
         * samples at the pitch they recorded them. */
        audio_set_pitch(t, PITCH_UNITY);
        audio_set_gain(t, 0x6000);
    }
    audio_set_master(TEST_MASTER);
    audio_start();

    printf("RESULT case=running note=stream is live and silent\n");

    display_init();
    input_init();
    printf("RESULT case=input note=pads 0-7 play, Function+pad selects, "
           "Select turns pitch, Volume turns level\n");


    /* Something to hear on the first press of Play, when the card had nothing. A four-on-the-floor kick
     * with hats on the offbeats says more about whether the timing is right
     * than an empty grid does. */
    if (!loaded_song) {
        for (uint32_t s = 0; s < 8; s += 2) {
            song_set_step(&song, 0, s, VELOCITY_DEFAULT, 0);
        }
        song_set_step(&song, 1, 4, VELOCITY_DEFAULT, 0);
        for (uint32_t s = 1; s < 8; s += 2) {
            song_set_step(&song, 2, s, 80, 0);
        }
    }

    /* LIVE or SEQ, toggled by a Function tap - engine/controls.py's design,
     * and independent of whether the transport is running.
     *
     * The first version tied this to the transport: pads played when stopped
     * and edited when running. That is two orthogonal things collapsed into
     * one, and it loses the thing a sampler is mostly for - playing pads over a
     * pattern that is already going.
     *
     * Tap versus hold is the same 250 ms the Python uses, and for the same
     * reason: Function held is a modifier (Function + pad selects a track),
     * Function tapped is a command. A press that did something else while it
     * was down was a modifier, whatever its duration. */
    /* Until when the screen shows the result of a save, and which result.
     *
     * Saving with no feedback at all means a missed button press and a failed
     * write look exactly the same from the outside - which is precisely the
     * position a player was left in. A timestamp rather than a sleep, because
     * blocking the loop for a flash would make sequenced hits late for the
     * sake of an animation. */
    absolute_time_t flash_until = get_absolute_time();
    bool flash_ok = false;
    /* Two lines of it, because "something happened" is only half an answer.
     * A save that says SAVED and a boot that names the song and tempo tell the
     * player which thing happened, which is the difference between feedback
     * and a light coming on. */
    char message[2][22] = {{0}, {0}};

    bool seq_mode = false;
    uint32_t function_down_ms = 0;
    bool function_used = false;

    uint8_t page = 0;
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

    /* Say so at boot when a song came off the card.
     *
     * Loading works and always did, but the badge starts in LIVE mode where the
     * grid shows pad activity rather than the pattern - so a song that loaded
     * perfectly looked exactly like one that had not. The player had no way to
     * tell the difference, and asked the right question about it. */
    if (loaded_song) {
        flash_ok = true;
        snprintf(message[0], sizeof(message[0]), "%s",
                 prefs.song[0] ? prefs.song : "session");
        snprintf(message[1], sizeof(message[1]), "%u BPM  %s", song.bpm,
                 song_division_name(&song));
        flash_until = make_timeout_time_ms(1600);
        printf("RESULT case=song loaded and showing\n");
    }

    absolute_time_t next_report = make_timeout_time_ms(2000);
    absolute_time_t next_frame = make_timeout_time_ms(33);
    uint32_t pages_written = 0;

    while (true) {
        console_pump();
        input_poll();
        seq_update(&seq);

        struct input_event event;
        while (input_next(&event)) {
            switch (event.kind) {
            case INPUT_KEY_DOWN:
                printf("RESULT case=key down=%u fn_held=%d\n", event.key,
                       input_held(KEY_FUNCTION) ? 1 : 0);
                if (event.key == KEY_FUNCTION) {
                    function_down_ms = to_ms_since_boot(get_absolute_time());
                    function_used = false;
                } else if (input_held(KEY_FUNCTION)) {
                    /* Anything pressed while Function is down makes it a
                     * modifier rather than a tap. */
                    function_used = true;
                    if (event.key <= KEY_PAD_LAST) {
                        selected = event.key;
                    } else if (event.key == KEY_SELECT_PUSH && mounted) {
                        /* Save. Deliberately a gesture engine/controls.py does
                         * not assign, because saving belongs in the menu and
                         * the menu does not exist yet - a placeholder on a real
                         * gesture would teach a habit that later has to be
                         * unlearned. */
                        enum songfile_result r =
                            songfile_save(song_path, &song);
                        if (r == SONGFILE_OK) {
                            const char *name =
                                prefs.song[0] ? prefs.song : "session";
                            strncpy(prefs.song, name, PREFS_NAME_MAX - 1);
                            prefs.song[PREFS_NAME_MAX - 1] = '\0';
                            prefs.volume = master;
                            prefs_save(&prefs);
                        }
                        flash_ok = (r == SONGFILE_OK);
                        snprintf(message[0], sizeof(message[0]), "%s",
                                 flash_ok ? "SAVED" : "SAVE FAILED");
                        snprintf(message[1], sizeof(message[1]), "%s",
                                 flash_ok ? (prefs.song[0] ? prefs.song
                                                           : "session")
                                          : songfile_result_name(r));
                        flash_until = make_timeout_time_ms(900);
                        printf("RESULT case=save path=%s result=%s\n",
                               song_path, songfile_result_name(r));
                    }
                } else if (event.key <= KEY_PAD_LAST) {
                    if (seq_mode) {
                        uint32_t step = page * STEPS_PER_PAGE + event.key;
                        bool on = song_toggle_step(&song, selected, step);
                        printf("RESULT case=edit track=%u step=%lu on=%d\n",
                               selected, (unsigned long)step, on ? 1 : 0);
                    } else {
                        /* LIVE: the pad plays, transport running or not. */
                        audio_trigger(event.key);
                        hits++;
                    }
                } else if (event.key == KEY_PLAY) {
                    seq_toggle(&seq);
                    printf("RESULT case=transport running=%d bpm=%u div=%s\n",
                           seq.running ? 1 : 0, song.bpm,
                           song_division_name(&song));
                } else if (event.key == KEY_SELECT_PUSH) {
                    uint32_t pages =
                        (song_length(&song) + STEPS_PER_PAGE - 1) /
                        STEPS_PER_PAGE;
                    page = (uint8_t)((page + 1) % (pages ? pages : 1));
                }
                break;

            case INPUT_KEY_UP:
                printf("RESULT case=key up=%u\n", event.key);
                if (event.key == KEY_FUNCTION && !function_used) {
                    uint32_t held =
                        to_ms_since_boot(get_absolute_time()) - function_down_ms;
                    if (held < 250) {
                        seq_mode = !seq_mode;
                        printf("RESULT case=mode mode=%s\n",
                               seq_mode ? "SEQ" : "LIVE");
                    }
                }
                break;

            case INPUT_SELECT_TURN:
                if (seq.running) {
                    /* Tempo while it plays, pitch while it does not - the knob
                     * means whatever the badge is currently doing. */
                    song_set_bpm(&song, song.bpm + event.delta);
                    printf("RESULT case=bpm bpm=%u\n", song.bpm);
                    break;
                }
                {
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
            if (!time_reached(flash_until)) {
                display_clear();
                if (!flash_ok) {
                    /* A failure gets a bar behind it, so it reads as wrong
                     * from across a room rather than only up close. */
                    display_fill_rect(0, 0, OLED_WIDTH, FONT_HEIGHT + 2, true);
                }
                display_text(2, 1, message[0], !flash_ok);
                display_text(2, FONT_HEIGHT + 5, message[1], true);
                pages_written += display_flush();
                next_frame = make_timeout_time_ms(33);
                continue;
            }

            uint32_t sounding = audio_active_mask();
            for (uint32_t t = 0; t < TRACK_COUNT; t++) {
                uint32_t x = t * 16;
                bool lit;
                if (seq_mode) {
                    /* Eight cells, eight steps of this page for the selected
                     * track, so the grid reads as the thing being edited - and
                     * the playhead is the cell the transport is on. */
                    uint32_t step = page * STEPS_PER_PAGE + t;
                    lit = song_is_on(&song, selected, step);
                    if (seq_step_of(&seq, selected) == step) {
                        display_fill_rect(x + 1, 1, 14, 2, true);
                    }
                } else {
                    lit = (sounding >> t) & 1u;
                }
                /* Filled while the track is sounding, outlined when idle, and
                 * the selected one keeps a gap down its middle so it reads as
                 * chosen whether or not it happens to be playing. */
                display_fill_rect(x + 1, 1, 14, 14, lit);
                if (!lit) {
                    display_rect(x + 1, 1, 14, 14, true);
                }
                if (!seq_mode && t == selected) {
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
                   "pages=%lu run=%d mode=%s tick=%lu seqhits=%lu\n",
                   (unsigned long)audio_blocks(),
                   (unsigned long)audio_underruns(),
                   (unsigned long)audio_worst_cycles(),
                   (unsigned long)audio_peak_voices(),
                   (unsigned long)hits, selected,
                   (unsigned long)pages_written, seq.running ? 1 : 0,
                   seq_mode ? "SEQ" : "LIVE",
                   (unsigned long)seq.tick, (unsigned long)seq.hits);
            next_report = make_timeout_time_ms(2000);
        }
    }
}
