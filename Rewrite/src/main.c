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
#include "pico/bootrom.h"
#if RT_USB_PROBATION
#include "pico/stdio_usb.h"
#include "tusb.h"
#endif
#include "pico/stdlib.h"

#include "audio.h"
#include "console.h"
#include "display.h"
#include "font.h"
#include "fat.h"
#include "input.h"
#include "kit.h"
#include "menu.h"
#include "midi.h"
#include "seq.h"
#include "song.h"
#include "prefs.h"
#include "songfile.h"
#include "anim.h"
#include "pixels.h"
#include "sync.h"
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
static struct menu menu;

static void build_song_path(void);

/* Typed commands, for measuring things that need the badge doing something but
 * do not need a person doing it. */
static volatile char console_command;

/* Which animation the strip falls back to when nothing is happening. At file
 * scope because the menu picks it and the render loop reads it, and those are
 * two different functions. */
static enum anim idle_anim = ANIM_PULSE;

/* Kept as a percentage as well as pushed to the strip, so the settings screen
 * can say what it is rather than reporting a level nobody chose in those
 * units. */
static uint8_t brightness_pct = PREFS_BRIGHTNESS_DEFAULT;

static void on_console_command(char c) {
    console_command = c;
}

/* --- what the knobs mean right now ----------------------------------------- *
 *
 * engine/controls.py resolves this rather than nesting conditions at each use,
 * and it is worth copying: the badge has two knobs and far more than two
 * things to adjust, so what a knob does depends entirely on what is being held.
 * Holding something scopes the knobs to it - a pad is a step in SEQ and a track
 * in LIVE, Function is the selected track, Play is the whole pattern.
 *
 * Written as one place that answers "what does turning this mean" so the
 * answer cannot drift between the two knobs.
 */
enum knob_target {
    KNOB_BPM = 0,      /* nothing held */
    KNOB_TRACK_PITCH,  /* Function */
    KNOB_TRACK_VOLUME, /* Function, volume knob */
    KNOB_LENGTH,       /* Play */
    KNOB_STEP_VELOCITY,/* a pad, in SEQ */
    KNOB_MASTER,       /* nothing held, volume knob */
    KNOB_NONE,
};

/* The lowest-numbered pad being held, or -1. Lowest rather than most recent
 * because two pads held at once is a chord the player did not mean as a
 * gesture, and picking the same one every time is at least predictable. */
static int32_t held_pad(void) {
    for (uint8_t key = KEY_PAD_FIRST; key <= KEY_PAD_LAST; key++) {
        if (input_held(key)) {
            return key;
        }
    }
    return -1;
}

/* What a track plays when the song has no opinion. sequencer.py's default kit,
 * at the paths the CircuitPython firmware keeps them. */
static const char *const DEFAULT_KIT[] = {
    "/samples/kick_crater.wav",
    "/samples/snare_kraken-head_1.wav",
    "/samples/hh_hats-closed_1.wav",
    "/samples/hh_hats-open_1.wav",
};

/* Until when the screen shows a result, and which result.
 *
 * A timestamp rather than a sleep: blocking the loop for a flash would make
 * sequenced hits late for the sake of an animation, and that is the one thing
 * this design spent four bugs getting right.
 *
 * Two lines, because "something happened" is only half an answer - a save that
 * says SAVED and names the file tells the player which thing happened, which is
 * the difference between feedback and a light coming on. */
static absolute_time_t flash_until;
static bool flash_ok;
static char message[2][22];
static int16_t master = 0x2000;

static void say(bool ok, uint32_t ms, const char *first, const char *second) {
    flash_ok = ok;
    snprintf(message[0], sizeof(message[0]), "%s", first);
    snprintf(message[1], sizeof(message[1]), "%s", second ? second : "");
    flash_until = make_timeout_time_ms(ms);
}

/* Reload every track from the song's kit.
 *
 * The arena is a bump allocator, so one track's sample cannot be swapped in
 * isolation - the space the old one held is not returnable. Resetting and
 * reloading all eight costs about a tenth of a second and is the honest
 * operation; anything cleverer would be a general allocator, which is the thing
 * this design exists without. */
static uint32_t reload_kit(struct song *song) {
    const char *const *fallback = DEFAULT_KIT;
    uint32_t fallback_count = count_of(DEFAULT_KIT);
    audio_arena_reset();
    uint32_t loaded = 0;
    for (uint8_t t = 0; t < TRACK_COUNT; t++) {
        const char *path = song->kit[t][0] ? song->kit[t] : NULL;
        if (path == NULL && t < fallback_count) {
            path = fallback[t];
        }
        if (path == NULL) {
            continue;
        }
        uint32_t frames_loaded = 0;
        if (kit_load_track(t, path, &frames_loaded) == KIT_OK) {
            loaded++;
            song_set_kit_path(song, t, path);
        }
    }
    return loaded;
}

/* Act on what the menu chose.
 *
 * A function rather than a block inside the event switch: the menu returns the
 * same actions whichever key triggered them, and Play and a future gesture
 * should not each carry their own copy of what LOAD SONG means. */
static void handle_menu_action(enum menu_action action) {
    char path[FAT_NAME_MAX + 24];

    switch (action) {
    case MENU_ACTION_LOAD_SONG: {
        strcpy(path, SONG_DIR);
        strcat(path, "/");
        strncat(path, menu.chosen, FAT_NAME_MAX - 1);
        enum songfile_result r = songfile_load(path, &song);
        if (r == SONGFILE_OK) {
            /* Remember it, so the next boot opens what was just chosen rather
             * than whatever was open before. */
            strncpy(prefs.song, menu.chosen, PREFS_NAME_MAX - 1);
            prefs.song[PREFS_NAME_MAX - 1] = '\0';
            char *dot = strrchr(prefs.song, '.');
            if (dot != NULL) {
                *dot = '\0';
            }
            build_song_path();
            prefs_save(&prefs);
            reload_kit(&song);
        }
        say(r == SONGFILE_OK, 1200,
            r == SONGFILE_OK ? "LOADED" : "LOAD FAILED", menu.chosen);
        break;
    }

    case MENU_ACTION_SAVE_SONG: {
        /* The name came from the name screen, so this may be a save-as onto a
         * file that does not exist yet. Point prefs at it first: the path is
         * built from prefs.song, and the next boot should open what was just
         * written rather than what was open before it. */
        strncpy(prefs.song, menu.chosen, PREFS_NAME_MAX - 1);
        prefs.song[PREFS_NAME_MAX - 1] = '\0';
        build_song_path();
        enum songfile_result r = songfile_save(song_path, &song);
        if (r == SONGFILE_OK) {
            prefs.volume = master;
            prefs_save(&prefs);
        }
        say(r == SONGFILE_OK, 900, r == SONGFILE_OK ? "SAVED" : "SAVE FAILED",
            prefs.song);
        break;
    }

    case MENU_ACTION_SET_DIVISION: {
        song_set_division(&song, (int32_t)menu.value);
        say(true, 700, "STEP", song_division_name(&song));
        break;
    }

    case MENU_ACTION_SET_LENGTH: {
        /* Every track, which is what makes this the pattern's length rather
         * than one track's. A player wanting them different reaches for Play
         * and the Select knob, where per-track length already lives. */
        for (uint8_t t = 0; t < TRACK_COUNT; t++) {
            song_set_length(&song, t, menu.value);
        }
        char line[22];
        snprintf(line, sizeof(line), "%lu STEPS", (unsigned long)menu.value);
        say(true, 700, "LENGTH", line);
        break;
    }

    case MENU_ACTION_SET_BRIGHTNESS: {
        brightness_pct = (uint8_t)menu.value;
        pixels_set_brightness((uint8_t)((menu.value * 255u) / 100u));
        prefs.brightness = brightness_pct;
        prefs_save(&prefs);
        char line[22];
        snprintf(line, sizeof(line), "%lu%%", (unsigned long)menu.value);
        say(true, 700, "BRIGHTNESS", line);
        break;
    }

    case MENU_ACTION_SET_SYNC: {
        seq_set_sync_ppqn(&seq, menu.value);
        char line[22];
        snprintf(line, sizeof(line), "%lu PER QUARTER",
                 (unsigned long)menu.value);
        say(true, 700, "SYNC", line);
        break;
    }

    case MENU_ACTION_DELETE_SONG: {
        strcpy(path, SONG_DIR);
        strcat(path, "/");
        strncat(path, menu.chosen, FAT_NAME_MAX - 1);
        bool gone = fat_delete(path);
        say(gone, 1200, gone ? "DELETED" : "DELETE FAILED", menu.chosen);
        printf("RESULT case=delete path=%s ok=%d\n", path, gone ? 1 : 0);
        break;
    }

    case MENU_ACTION_SET_ANIM:
        /* Set by handle_menu_action, read by the render below - which is why
         * idle_anim is a file-scope variable rather than a local: the menu
         * hands back a choice and cannot reach into the loop that draws. */
        idle_anim = (enum anim)menu.anim;
        say(true, 700, "LIGHTS", anim_name(idle_anim));
        break;

    case MENU_ACTION_SET_SAMPLE: {
        strcpy(path, "/samples/");
        strncat(path, menu.chosen, FAT_NAME_MAX - 1);
        song_set_kit_path(&song, menu.track, path);
        uint32_t loaded = reload_kit(&song);
        char which[16];
        snprintf(which, sizeof(which), "TRACK %u", menu.track + 1);
        say(loaded > 0, 1200, which, menu.chosen);
        break;
    }

    default:
        break;
    }
}

static void build_song_path(void) {
    const char *name = prefs.song[0] ? prefs.song : "session";
    strcpy(song_path, SONG_DIR);
    strcat(song_path, "/");
    strncat(song_path, name, PREFS_NAME_MAX - 1);
    strcat(song_path, SONG_SUFFIX);
}

int main(void) {
    console_begin("rt-phase1");

#if RT_USB_PROBATION
    /* See RT_USB_PROBATION in CMakeLists.txt. A bench build that cannot get
     * itself onto the bus puts itself back in the bootloader rather than
     * sitting there playing to nobody, unreachable by every tool that talks to
     * it over the very thing that is broken. */
    if (!stdio_usb_connected() && !tud_mounted()) {
        absolute_time_t give_up = make_timeout_time_ms(RT_USB_PROBATION_MS);
        while (!tud_mounted()) {
            if (time_reached(give_up)) {
                reset_usb_boot(0, 0);
            }
            watchdog_update();
            tight_loop_contents();
        }
    }
#endif

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
        /* Apply it before anything can light up, so the panel never flashes at
         * a brightness the player turned down - and, on a badge whose rail is
         * shared with the card and the amplifier, never draws more than they
         * asked it to even for one frame. */
        brightness_pct = prefs.brightness;
        pixels_set_brightness((uint8_t)((brightness_pct * 255u) / 100u));
        build_song_path();
        enum songfile_result r = songfile_load(song_path, &song);
        printf("RESULT case=song path=%s result=%s bpm=%u div=%s empty=%d "
               "prefs=%d volume=%ld bright=%u\n",
               song_path, songfile_result_name(r), song.bpm,
               song_division_name(&song), song_is_empty(&song) ? 1 : 0,
               prefs.loaded ? 1 : 0, (long)prefs.volume,
               prefs.brightness);
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

    /* The way back, which must not depend on USB.
     *
     * Everything else about recovering this badge - flash.py's enter_bootsel,
     * the console's 'B' - goes down the CDC serial port. That is fine until the
     * thing being changed is the USB descriptor itself, at which point a
     * mistake costs a device that runs, ignores the host, and cannot be told to
     * do anything about it.
     *
     * Holding Function at power-on goes straight to the bootloader instead. No
     * USB, no console, no firmware: just a pin read and a jump. */
    if (input_read_key_now(KEY_FUNCTION)) {
        reset_usb_boot(0, 0);
    }

    sync_init();
    pixels_init();
    midi_init();
    console_set_command_hook(on_console_command);
    menu_close(&menu);
    printf("RESULT case=input note=pads 0-7 play, Function+pad selects, "
           "Select click opens the menu, Play enters, Function backs out\n");


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

    bool seq_mode = false;
    uint32_t function_down_ms = 0;
    bool function_used = false;
    /* Play is a modifier as well as the transport button, so like Function it
     * acts on release and only if nothing was pressed against it. The cost is
     * the length of a tap before the transport moves, which engine/controls.py
     * names as a deliberate trade - the alternative is that holding Play to
     * erase or change pages also starts the song. */
    bool play_used = false;
    /* Recording arm. Quantise strength lives on the transport, because it is
     * applied when a hit is scheduled rather than when it is captured. */
    bool armed = false;

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
    master = TEST_MASTER;
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
        char typed = console_command;
        if (typed != '\0') {
            console_command = '\0';
            if (typed == 'p') {
                seq_toggle(&seq);
                printf("RESULT case=transport running=%d bpm=%u\n",
                       seq.running ? 1 : 0, song.bpm);
            } else if (typed == 'r') {
                /* Put something on the grid to hear, so a headless timing run
                 * is measuring a pattern rather than silence. */
                for (uint32_t t = 0; t < TRACK_COUNT; t++) {
                    song_set_length(&song, (uint8_t)t, 8);
                    for (uint32_t st = 0; st < 8; st += 4) {
                        song_set_step(&song, (uint8_t)t, st + t % 4,
                                      VELOCITY_DEFAULT, 0);
                    }
                }
                printf("RESULT case=pattern filled\n");
            }
        }

        input_poll();

        /* Drain the sync input before scheduling, so a pulse that arrived
         * during the last pass has already moved the clock by the time this
         * pass books ticks against it. */
        uint32_t pulse_us;
        while (sync_take_pulse(&pulse_us)) {
            seq_external_pulse(&seq, pulse_us, seq.sync_ppqn);
        }

        midi_pump();

        /* And MIDI, which is a clock too - at 24 a quarter note, told to the
         * transport explicitly because the jack's rate and the standard's can
         * differ and only one of them is a cable. */
        struct midi_message incoming;
        while (midi_receive(&incoming)) {
            switch (incoming.kind) {
            case MIDI_CLOCK:
                seq_external_pulse(&seq, time_us_32(), MIDI_CLOCK_PPQN);
                break;
            case MIDI_START:
                if (!seq.running) {
                    seq_start(&seq);
                }
                break;
            case MIDI_CONTINUE:
                if (!seq.running) {
                    seq_start(&seq);
                }
                break;
            case MIDI_STOP:
                if (seq.running) {
                    seq_stop(&seq);
                }
                break;
            case MIDI_NOTE_ON: {
                /* 36 + track, the range this firmware sends on. Anything
                 * outside it belongs to another instrument on the same
                 * cable. */
                int32_t track = (int32_t)incoming.data1 - MIDI_NOTE_BASE;
                if (track >= 0 && track < TRACK_COUNT &&
                    !song.muted[track]) {
                    audio_trigger((uint8_t)track);
                    hits++;
                }
                break;
            }
            default:
                break;
            }
        }

        seq_update(&seq);

        struct input_event event;
        while (input_next(&event)) {
            switch (event.kind) {
            case INPUT_KEY_DOWN:
                printf("RESULT case=key down=%u fn_held=%d\n", event.key,
                       input_held(KEY_FUNCTION) ? 1 : 0);
                if (event.key == KEY_FUNCTION) {
                    if (menu_is_open(&menu)) {
                        /* Back. Function is the modifier everywhere else, so it
                         * is the key that already means "not the main thing" -
                         * which is what back is. */
                        menu_back(&menu);
                        break;
                    }
                    function_down_ms = to_ms_since_boot(get_absolute_time());
                    function_used = false;
                } else if (input_held(KEY_FUNCTION)) {
                    /* Anything pressed while Function is down makes it a
                     * modifier rather than a tap. */
                    function_used = true;
                    if (event.key <= KEY_PAD_LAST) {
                        selected = event.key;
                    } else if (event.key == KEY_PLAY) {
                        play_used = true;
                        armed = !armed;
                        say(true, 700, armed ? "RECORD ARMED" : "RECORD OFF",
                            armed ? "PLAY PADS TO RECORD" : "");
                        printf("RESULT case=arm armed=%d\n", armed ? 1 : 0);
                    } else if (event.key == KEY_VOLUME_PUSH) {
                        for (uint32_t i = 0; i < MAX_STEPS; i++) {
                            song_clear_step(&song, selected, i);
                        }
                        char line[22];
                        snprintf(line, sizeof(line), "TRACK %u", selected + 1);
                        say(true, 700, "CLEARED", line);
                        printf("RESULT case=clear track=%u\n", selected);
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
                    if (input_held(KEY_PLAY)) {
                        play_used = true;
                    }
                    if (input_held(KEY_PLAY) && seq_mode) {
                        /* Play held turns the pads into page buttons, which is
                         * the only way to reach step 64 on eight pads. */
                        uint32_t pages = (song_length(&song) +
                                          STEPS_PER_PAGE - 1) / STEPS_PER_PAGE;
                        if (event.key < pages) {
                            page = event.key;
                            char line[22];
                            snprintf(line, sizeof(line), "%u OF %lu", page + 1,
                                     (unsigned long)pages);
                            say(true, 500, "PAGE", line);
                        }
                    } else if (seq_mode) {
                        uint32_t step = page * STEPS_PER_PAGE + event.key;
                        bool on = song_toggle_step(&song, selected, step);
                        printf("RESULT case=edit track=%u step=%lu on=%d\n",
                               selected, (unsigned long)step, on ? 1 : 0);
                    } else if (input_held(KEY_PLAY)) {
                        /* Play held in LIVE is erase, not trigger. Nothing
                         * happens on the press itself - the clearing is done
                         * in the loop below, as the playhead reaches each
                         * step, so it erases what is passing rather than the
                         * whole track at once. */
                    } else {
                        /* LIVE: the pad plays, transport running or not. It
                         * sounds first and is written down second - a hit the
                         * player hears late because the write went first is a
                         * worse trade than a step recorded a few microseconds
                         * after it sounded. */
                        if (!song.muted[event.key]) {
                            audio_trigger(event.key);
                            midi_send_note_on(
                                (uint8_t)(MIDI_NOTE_BASE + event.key),
                                VELOCITY_DEFAULT);
                            hits++;
                        }

                        uint32_t step;
                        int32_t offset;
                        if (armed && seq_now(&seq, event.key, &step, &offset)) {
                            /* Stored as played. Quantise is applied on the way
                             * back out, in seq_effective_offset, so the knob
                             * can be turned down again afterwards and the feel
                             * is still there to recover. */
                            int32_t limit = song_max_offset(&song);
                            if (offset > limit) {
                                offset = limit;
                            }
                            if (offset < -limit) {
                                offset = -limit;
                            }
                            song_set_step(&song, event.key, step,
                                          VELOCITY_DEFAULT, offset);
                            printf("RESULT case=record track=%u step=%lu "
                                   "offset=%ld\n",
                                   event.key, (unsigned long)step, (long)offset);
                        }
                    }
                } else if (event.key == KEY_PLAY) {
                    if (menu_is_open(&menu)) {
                        /* Marked used, not just guarded on release: entering a
                         * menu item can close the menu, and the release would
                         * then find no menu open and start the song. */
                        play_used = true;
                        handle_menu_action(menu_enter(&menu));
                        break;
                    }
                    /* The transport moves on release - see play_used. */
                    play_used = false;
                } else if (event.key == KEY_SELECT_PUSH) {
                    if (!menu_is_open(&menu)) {
                        menu_open(&menu);
                        /* After opening, not before: menu_open clears the
                         * name along with the rest of the state. Seeded with
                         * whatever is currently open, so re-saving under the
                         * same name is two clicks rather than sixteen turns
                         * of a knob. */
                        menu_set_name(&menu, prefs.song[0] ? prefs.song
                                                           : "SESSION");
                    }
                } else if (event.key == KEY_VOLUME_PUSH) {
                    /* Mute lives on the song, and each thing that triggers
                     * honours it: seq_update skips booking a muted track, and
                     * the live pad below refuses to sound one. Not a mixer
                     * flag, because the sequencer's version also saves it the
                     * work of reserving a voice for silence. */
                    song.muted[selected] = !song.muted[selected];
                    char line[22];
                    snprintf(line, sizeof(line), "TRACK %u", selected + 1);
                    say(true, 600, song.muted[selected] ? "MUTED" : "UNMUTED",
                        line);
                }
                break;

            case INPUT_KEY_UP:
                printf("RESULT case=key up=%u\n", event.key);
                if (event.key == KEY_PLAY && !play_used &&
                    !menu_is_open(&menu)) {
                    seq_toggle(&seq);
                    /* Tell whatever else is on the cable. Only when this badge
                     * is the one keeping time - a slave echoing start and stop
                     * back at its master is how a loop starts. */
                    if (!seq.external) {
                        if (seq.running) {
                            midi_send_start();
                        } else {
                            midi_send_stop();
                        }
                    }
                    printf("RESULT case=transport running=%d bpm=%u div=%s\n",
                           seq.running ? 1 : 0, song.bpm,
                           song_division_name(&song));
                }
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

            case INPUT_SELECT_TURN: {
                if (menu_is_open(&menu)) {
                    menu_turn(&menu, event.delta);
                    break;
                }
                int32_t pad = held_pad();
                if (input_held(KEY_FUNCTION)) {
                    /* The selected track's pitch, a semitone a click, as
                     * 1090/1029 rather than a table: 2^(1/12) to five decimal
                     * places in integers this chip multiplies unaided. */
                    function_used = true;
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
                    char line[22];
                    snprintf(line, sizeof(line), "T%u  %lu%%", selected + 1,
                             (unsigned long)((uint64_t)p * 100u / PITCH_UNITY));
                    say(true, 700, "PITCH", line);
                } else if (input_held(KEY_PLAY)) {
                    play_used = true;
                    /* The selected track's length, which is what makes tracks
                     * of different lengths a polyrhythm rather than a bug. */
                    int32_t length = (int32_t)song.lengths[selected] + event.delta;
                    song_set_length(&song, selected, (uint32_t)(length < 1 ? 1 : length));
                    char line[22];
                    snprintf(line, sizeof(line), "T%u  %u STEPS", selected + 1,
                             song.lengths[selected]);
                    say(true, 700, "LENGTH", line);
                } else if (pad >= 0) {
                    /* engine/controls.py gives this "pitch: of that step in
                     * SEQ, that track in LIVE" - but per-step pitch does not
                     * exist in the song model, in either firmware, and the
                     * Python's own sequencer notes it as not yet done. So both
                     * modes move the track's pitch, which is the half that is
                     * actually implementable, and nothing here pretends
                     * otherwise. */
                    uint32_t p = pitch[pad];
                    int32_t steps = event.delta;
                    while (steps > 0 && p < PITCH_UNITY * 4) {
                        p = (uint32_t)(((uint64_t)p * 1090u) / 1029u);
                        steps--;
                    }
                    while (steps < 0 && p > PITCH_UNITY / 4) {
                        p = (uint32_t)(((uint64_t)p * 1029u) / 1090u);
                        steps++;
                    }
                    pitch[pad] = p;
                    audio_set_pitch((uint8_t)pad, p);
                    char line[22];
                    snprintf(line, sizeof(line), "T%ld  %lu%%", (long)pad + 1,
                             (unsigned long)((uint64_t)p * 100u / PITCH_UNITY));
                    say(true, 700, "PITCH", line);
                } else {
                    song_set_bpm(&song, song.bpm + event.delta);
                    char line[22];
                    snprintf(line, sizeof(line), "%u", song.bpm);
                    say(true, 600, "TEMPO", line);
                }
                break;
            }

            case INPUT_VOLUME_TURN: {
                if (menu_is_open(&menu)) {
                    menu_turn_volume(&menu, event.delta);
                    break;
                }
                int32_t pad = held_pad();
                if (input_held(KEY_FUNCTION) || (pad >= 0 && !seq_mode)) {
                    /* A track's own level: Function scopes to the selected
                     * track, a held pad in LIVE to that one. */
                    uint8_t track = input_held(KEY_FUNCTION) ? selected
                                                             : (uint8_t)pad;
                    if (input_held(KEY_FUNCTION)) {
                        function_used = true;
                    }
                    int32_t level = (int32_t)song.volume_q12[track] +
                                    event.delta * 256;
                    if (level < 0) {
                        level = 0;
                    }
                    if (level > 8192) {
                        level = 8192; /* 2.0, the Python's ceiling */
                    }
                    song.volume_q12[track] = (uint16_t)level;
                    audio_set_gain(track, (int16_t)((level * 0x7FFF) / 8192));
                    char line[22];
                    snprintf(line, sizeof(line), "T%u  %ld%%", track + 1,
                             (long)(level * 100 / 4096));
                    say(true, 700, "LEVEL", line);
                } else if (input_held(KEY_PLAY)) {
                    play_used = true;
                    int32_t q = (int32_t)seq.strength + event.delta;
                    seq.strength =
                        (uint8_t)(q < 0 ? 0 : (q > STRENGTH_MAX ? STRENGTH_MAX
                                                                : q));
                    char line[22];
                    snprintf(line, sizeof(line), "%u%%",
                             seq.strength * 100u / STRENGTH_MAX);
                    say(true, 700, "QUANTIZE", line);
                } else if (pad >= 0 && seq_mode) {
                    /* That step's velocity. Deliberately not its offset: no
                     * gesture in either firmware edits an offset by hand, and
                     * that is what makes a default quantise strength of 100%
                     * coherent - offsets are a record of how the pattern was
                     * played, and the knob decides how much of that to keep.
                     * An offset the player dialled in would be snapped away by
                     * the same setting, which is a trap. */
                    uint32_t step = page * STEPS_PER_PAGE + (uint32_t)pad;
                    int32_t v = song_velocity(&song, selected, step) +
                                event.delta;
                    if (v > VELOCITY_MAX) {
                        v = VELOCITY_MAX;
                    }
                    if (v < VELOCITY_OFF) {
                        v = VELOCITY_OFF;
                    }
                    song_set_step(&song, selected, step, (uint8_t)v,
                                  song_offset(&song, selected, step));
                    char line[22];
                    snprintf(line, sizeof(line), "S%lu  VEL %ld",
                             (unsigned long)(step + 1), (long)v);
                    say(true, 700, "STEP", line);
                } else {
                    int32_t next = master + event.delta * 0x0400;
                    if (next > 0x7FFF) {
                        next = 0x7FFF;
                    }
                    if (next < 0) {
                        next = 0;
                    }
                    master = (int16_t)next;
                    audio_set_master(master);
                    char line[22];
                    snprintf(line, sizeof(line), "%ld%%",
                             (long)((int32_t)master * 100 / 0x7FFF));
                    say(true, 600, "VOLUME", line);
                }
                break;
            }

            default:
                break;
            }
        }

        /* Live erase: while Play and a pad are both held in LIVE, the step
         * under that track's playhead is cleared as it goes by. Held down over
         * a bar it wipes the track; tapped, it takes out just what was
         * sounding - which is the point, and why this is not a clear button. */
        if (!menu_is_open(&menu) && !seq_mode && seq.running &&
            input_held(KEY_PLAY)) {
            for (uint8_t t = KEY_PAD_FIRST; t <= KEY_PAD_LAST; t++) {
                if (input_held(t)) {
                    song_clear_step(&song, t, seq_step_of(&seq, t));
                }
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

            if (menu_is_open(&menu)) {
                /* Refreshed every frame rather than on each change: it is four
                 * bytes and a compare, and the alternative is remembering to
                 * do it at every place a setting can move - including the
                 * gestures, which do not go through the menu at all. */
                struct menu_context context = {
                    .division = song.division,
                    .length = song.lengths[selected],
                    .brightness_pct = brightness_pct,
                    .sync_ppqn = seq.sync_ppqn,
                };
                menu_set_context(&menu, &context);
                menu_draw(&menu);
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
                /* A muted track is struck through. Not dimmed - the panel has
                 * one bit a pixel and no dim to give - and not blank, because
                 * blank is what an empty track already looks like and the two
                 * mean very different things. */
                if (song.muted[t]) {
                    display_fill_rect(x + 3, 7, 10, 2, !lit);
                }
            }

            /* Armed to record: a blinking mark in the strip between the pitch
             * and level bars, which is the only space left. Blinking because a
             * static dot on a panel this dense reads as part of the furniture,
             * and arming is a state that must not be forgotten. */
            if (armed && (to_ms_since_boot(get_absolute_time()) / 400u) % 2u) {
                /* No label beside it: the band between the two bars is four
                 * pixels tall and the type is nine, so a word here would run
                 * straight through the level bar. A blinking dot in a fixed
                 * place is the one indicator that needs no legend. */
                display_fill_rect(0, 22, 4, 4, true);
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

            /* --- the strip -------------------------------------------------
             *
             * Two things to show and only ten pixels, so they take turns: the
             * pads say what the instrument is doing whenever it is doing
             * something, and the animation has the strip the rest of the time.
             * Showing both at once would mean neither read clearly. */
            struct rgb strip[NEOPIXEL_COUNT];
            bool busy = seq.running || sounding != 0 || held_pad() >= 0 ||
                        input_held(KEY_FUNCTION) || input_held(KEY_PLAY);
            if (busy) {
                anim_render(ANIM_OFF, 0, 0, strip);
                for (uint8_t t = 0; t < TRACK_COUNT; t++) {
                    struct rgb c = {0, 0, 0};
                    if (seq_mode) {
                        uint32_t step = page * STEPS_PER_PAGE + t;
                        if (seq.running && seq_step_of(&seq, selected) == step) {
                            c = (struct rgb){255, 0, 255}; /* PLAYHEAD */
                        } else if (song_is_on(&song, selected, step)) {
                            c = (struct rgb){28, 28, 28}; /* STEP_ON */
                        }
                    } else if ((sounding >> t) & 1u) {
                        c = (struct rgb){255, 255, 255}; /* TRACK_FLASH */
                    } else if (song.muted[t]) {
                        c = (struct rgb){40, 0, 0}; /* TRACK_MUTED */
                    } else if (t == selected) {
                        c = (struct rgb){255, 0, 255}; /* TRACK_SELECTED */
                    } else if (song.kit[t][0] != '\0') {
                        c = (struct rgb){28, 0, 28}; /* TRACK_LOADED */
                    }
                    strip[PIXEL_FOR_PAD[t]] = c;
                }
                /* Play: what the transport is doing. Function: which view, or
                 * that the clock is somebody else's. */
                strip[PIXEL_PLAY] = seq.running ? (struct rgb){0, 120, 0}
                                                : (struct rgb){255, 0, 255};
                if (armed) {
                    strip[PIXEL_PLAY] = (struct rgb){255, 0, 0};
                }
                strip[PIXEL_FUNCTION] =
                    seq.external ? (struct rgb){60, 0, 60}
                                 : (seq_mode ? (struct rgb){255, 255, 255}
                                             : (struct rgb){255, 0, 255});
            } else {
                anim_render(idle_anim, seq_display_tick(&seq), 255, strip);
            }
            for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
                pixels_set(i, strip[i].r, strip[i].g, strip[i].b);
            }
            pixels_show();
        }

        if (time_reached(next_report)) {
            printf("RESULT case=stats blocks=%lu underruns=%lu "
                   "worst_cycles=%lu peak=%lu hits=%lu selected=%u "
                   "pages=%lu run=%d mode=%s tick=%lu seqhits=%lu "
                   "clock=%s bpm=%lu syncin=%lu syncbad=%lu "
                   "syncout=%lu syncerr=%lu syncmiss=%lu pixframes=%lu "
                   "midiin=%lu midiout=%lu midiclk=%lu mididrop=%lu\n",
                   (unsigned long)audio_blocks(),
                   (unsigned long)audio_underruns(),
                   (unsigned long)audio_worst_cycles(),
                   (unsigned long)audio_peak_voices(),
                   (unsigned long)hits, selected,
                   (unsigned long)pages_written, seq.running ? 1 : 0,
                   seq_mode ? "SEQ" : "LIVE",
                   (unsigned long)seq.tick, (unsigned long)seq.hits,
                   seq.external ? "ext" : "int",
                   (unsigned long)seq_effective_bpm(&seq),
                   (unsigned long)sync_pulses_in(),
                   (unsigned long)sync_pulses_rejected(),
                   (unsigned long)sync_pulses_out(),
                   (unsigned long)sync_out_worst_error_us(),
                   (unsigned long)sync_pulses_missed(),
                   (unsigned long)pixels_frames_sent(),
                   (unsigned long)midi_bytes_in(),
                   (unsigned long)midi_bytes_out(),
                   (unsigned long)midi_clocks_sent(),
                   (unsigned long)midi_clocks_dropped());
            next_report = make_timeout_time_ms(2000);
        }
    }
}
