/* Host tests for the menu.
 *
 * Navigation, bounds and what a click means are pure logic, and pure logic is
 * better enumerated on a laptop than poked at with a knob. What the badge is
 * needed for is whether it reads well - which is judgement, not a test.
 *
 * The card is stubbed, so the directory can be made empty, exactly as long as
 * the window, or longer, and each case checked rather than whichever one the
 * real card happens to hold today.
 */

#include <stdio.h>
#include <string.h>

#include "menu.h"
#include "song.h"

/* --- the card, faked ------------------------------------------------------- */

static const char *songs[8];
static uint32_t song_count;
static const char *samples[8];
static uint32_t sample_count;

bool fat_list(const char *path, uint32_t index, char *name_out,
              uint32_t name_size, bool *is_dir, uint32_t *size_out) {
    const char **list = NULL;
    uint32_t count = 0;
    if (strcmp(path, "/songs") == 0) {
        list = songs;
        count = song_count;
    } else if (strcmp(path, "/samples") == 0) {
        list = samples;
        count = sample_count;
    } else {
        return false;
    }
    if (index >= count) {
        return false;
    }
    strncpy(name_out, list[index], name_size - 1);
    name_out[name_size - 1] = '\0';
    *is_dir = false;
    *size_out = 100;
    return true;
}

/* --- the screen, ignored --------------------------------------------------- */

void display_clear(void) {}
void display_pixel(uint32_t x, uint32_t y, bool on) { (void)x; (void)y; (void)on; }
void display_fill_rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, bool on) {
    (void)x; (void)y; (void)w; (void)h; (void)on;
}
uint32_t display_text(uint32_t x, uint32_t y, const char *text, bool on) {
    (void)y; (void)text; (void)on;
    return x;
}

/* --- harness --------------------------------------------------------------- */

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

static void test_root_and_back(void) {
    struct menu menu;
    menu_open(&menu);
    check(menu_is_open(&menu), "opens");
    check(menu.screen == MENU_ROOT, "starts at the root");
    check(menu.count == 3, "root has three items");

    menu_back(&menu);
    check(!menu_is_open(&menu), "back from the root closes it");
}

static void test_selection_stops_at_the_ends(void) {
    struct menu menu;
    menu_open(&menu);

    menu_turn(&menu, -5);
    check(menu.index == 0, "cannot go above the first item");

    menu_turn(&menu, 99);
    check(menu.index == menu.count - 1, "cannot go past the last");

    /* Deliberately not wrapping: on a three-row window, a list that wraps
     * makes "how far down am I" unanswerable. */
    menu_turn(&menu, 1);
    check(menu.index == menu.count - 1, "stops rather than wrapping");
}

static void test_loading_a_song(void) {
    songs[0] = "beat.song";
    songs[1] = "other.song";
    song_count = 2;

    struct menu menu;
    menu_open(&menu);
    check(menu_click(&menu) == MENU_ACTION_NONE, "LOAD SONG opens a list");
    check(menu.screen == MENU_SONGS, "on the song list");
    check(menu.count == 2, "sees both songs");

    menu_turn(&menu, 1);
    check(menu_click(&menu) == MENU_ACTION_LOAD_SONG, "picking asks for a load");
    check(strcmp(menu.chosen, "other.song") == 0, "the one that was selected");
    check(!menu_is_open(&menu), "and closes behind itself");
}

static void test_assigning_a_sample(void) {
    samples[0] = "kick.wav";
    samples[1] = "snare.wav";
    samples[2] = "hat.wav";
    sample_count = 3;

    struct menu menu;
    menu_open(&menu);
    menu_turn(&menu, 2); /* TRACK SAMPLE */
    check(menu_click(&menu) == MENU_ACTION_NONE, "asks which track first");
    check(menu.screen == MENU_TRACKS, "on the track list");
    check(menu.count == TRACK_COUNT, "one entry per track");

    menu_turn(&menu, 3);
    check(menu_click(&menu) == MENU_ACTION_NONE, "choosing a track opens samples");
    check(menu.track == 3, "remembers which track");
    check(menu.screen == MENU_SAMPLES, "on the sample list");

    menu_turn(&menu, 2);
    check(menu_click(&menu) == MENU_ACTION_SET_SAMPLE, "picking assigns");
    check(strcmp(menu.chosen, "hat.wav") == 0, "the selected sample");
    check(menu.track == 3, "for the track chosen earlier");
}

static void test_back_unwinds_one_screen(void) {
    struct menu menu;
    menu_open(&menu);
    menu_turn(&menu, 2);
    menu_click(&menu); /* tracks */
    menu_click(&menu); /* samples */
    check(menu.screen == MENU_SAMPLES, "three deep");

    menu_back(&menu);
    check(menu.screen == MENU_TRACKS, "back to the tracks");
    menu_back(&menu);
    check(menu.screen == MENU_ROOT, "back to the root");
    menu_back(&menu);
    check(!menu_is_open(&menu), "and out");
}

static void test_an_empty_directory(void) {
    song_count = 0;

    struct menu menu;
    menu_open(&menu);
    menu_click(&menu); /* LOAD SONG */
    check(menu.count == 0, "an empty directory has no items");

    /* The interesting part: none of this may misbehave with nothing to show. */
    menu_turn(&menu, 1);
    check(menu.index == 0, "turning does nothing");
    check(menu_click(&menu) == MENU_ACTION_NONE, "clicking chooses nothing");
    menu_draw(&menu, 0);
    check(true, "drawing an empty list does not crash");
    menu_back(&menu);
    check(menu.screen == MENU_ROOT, "and back still works");
}

static void test_window_scrolls_with_a_long_list(void) {
    for (uint32_t i = 0; i < 8; i++) {
        samples[i] = "sample.wav";
    }
    sample_count = 8;

    struct menu menu;
    menu_open(&menu);
    menu_turn(&menu, 2);
    menu_click(&menu);
    menu_click(&menu); /* samples, 8 of them */

    menu_draw(&menu, 0);
    check(menu.window_first == 0, "starts at the top");

    menu_turn(&menu, 7);
    menu_draw(&menu, 0);
    check(menu.index == 7, "at the last item");
    check(menu.window_first == sample_count - MENU_VISIBLE,
          "the window followed it to the bottom");
}

int main(void) {
    test_root_and_back();
    test_selection_stops_at_the_ends();
    test_loading_a_song();
    test_assigning_a_sample();
    test_back_unwinds_one_screen();
    test_an_empty_directory();
    test_window_scrolls_with_a_long_list();

    if (failures == 0) {
        printf("ok - all menu tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
