/* See menu.h. */

#include <stdio.h>
#include <string.h>

#include "display.h"
#include "font.h"
#include "menu.h"
#include "songfile.h"

#define SAMPLE_DIR "/samples"

static const char *const ROOT_ITEMS[] = {
    "LOAD SONG",
    "SAVE SONG",
    "TRACK SAMPLE",
};
#define ROOT_COUNT (sizeof(ROOT_ITEMS) / sizeof(ROOT_ITEMS[0]))

/* How many files a directory holds, by walking it.
 *
 * Counted rather than remembered: the card is not this firmware's to assume
 * about, and a song saved between one look and the next should appear. */
static uint32_t count_files(const char *directory) {
    char name[FAT_NAME_MAX];
    bool is_dir = false;
    uint32_t size = 0, files = 0;
    for (uint32_t i = 0; i < 256; i++) {
        if (!fat_list(directory, i, name, sizeof(name), &is_dir, &size)) {
            break;
        }
        if (!is_dir) {
            files++;
        }
    }
    return files;
}

/* The nth file, skipping directories, so indices the menu shows match indices
 * it can act on. */
static bool nth_file(const char *directory, uint32_t want, char *out,
                     uint32_t out_size) {
    char name[FAT_NAME_MAX];
    bool is_dir = false;
    uint32_t size = 0, files = 0;
    for (uint32_t i = 0; i < 256; i++) {
        if (!fat_list(directory, i, name, sizeof(name), &is_dir, &size)) {
            return false;
        }
        if (is_dir) {
            continue;
        }
        if (files == want) {
            strncpy(out, name, out_size - 1);
            out[out_size - 1] = '\0';
            return true;
        }
        files++;
    }
    return false;
}

static const char *screen_directory(const struct menu *menu) {
    switch (menu->screen) {
    case MENU_SONGS:
        return SONG_DIR;
    case MENU_SAMPLES:
        return SAMPLE_DIR;
    default:
        return NULL;
    }
}

static void recount(struct menu *menu) {
    const char *directory = screen_directory(menu);
    if (directory != NULL) {
        menu->count = count_files(directory);
    } else if (menu->screen == MENU_ROOT) {
        menu->count = ROOT_COUNT;
    } else if (menu->screen == MENU_TRACKS) {
        menu->count = TRACK_COUNT;
    } else {
        menu->count = 0;
    }
    if (menu->index >= menu->count) {
        menu->index = menu->count ? menu->count - 1 : 0;
    }
    menu->window_valid = false;
}

static void enter(struct menu *menu, enum menu_screen screen) {
    menu->screen = screen;
    menu->index = 0;
    recount(menu);
}

void menu_open(struct menu *menu) {
    memset(menu, 0, sizeof(*menu));
    enter(menu, MENU_ROOT);
}

void menu_close(struct menu *menu) {
    menu->screen = MENU_CLOSED;
    menu->window_valid = false;
}

bool menu_is_open(const struct menu *menu) {
    return menu->screen != MENU_CLOSED;
}

void menu_turn(struct menu *menu, int32_t delta) {
    if (menu->count == 0) {
        return;
    }
    int32_t next = (int32_t)menu->index + delta;
    /* Stop at the ends rather than wrapping. A list that wraps makes "how far
     * down am I" unanswerable on a three-row window. */
    if (next < 0) {
        next = 0;
    }
    if (next >= (int32_t)menu->count) {
        next = (int32_t)menu->count - 1;
    }
    if ((uint32_t)next != menu->index) {
        menu->index = (uint32_t)next;
        menu->window_valid = false;
    }
}

void menu_back(struct menu *menu) {
    switch (menu->screen) {
    case MENU_SONGS:
    case MENU_TRACKS:
        enter(menu, MENU_ROOT);
        break;
    case MENU_SAMPLES:
        enter(menu, MENU_TRACKS);
        break;
    default:
        menu_close(menu);
        break;
    }
}

enum menu_action menu_click(struct menu *menu) {
    switch (menu->screen) {
    case MENU_ROOT:
        if (menu->index == 0) {
            enter(menu, MENU_SONGS);
        } else if (menu->index == 1) {
            menu_close(menu);
            return MENU_ACTION_SAVE_SONG;
        } else {
            enter(menu, MENU_TRACKS);
        }
        return MENU_ACTION_NONE;

    case MENU_SONGS:
        if (nth_file(SONG_DIR, menu->index, menu->chosen,
                     sizeof(menu->chosen))) {
            menu_close(menu);
            return MENU_ACTION_LOAD_SONG;
        }
        return MENU_ACTION_NONE;

    case MENU_TRACKS:
        menu->track = (uint8_t)menu->index;
        enter(menu, MENU_SAMPLES);
        return MENU_ACTION_NONE;

    case MENU_SAMPLES:
        if (nth_file(SAMPLE_DIR, menu->index, menu->chosen,
                     sizeof(menu->chosen))) {
            menu_close(menu);
            return MENU_ACTION_SET_SAMPLE;
        }
        return MENU_ACTION_NONE;

    default:
        return MENU_ACTION_NONE;
    }
}

/* --- drawing ---------------------------------------------------------------- */

static void label_for(const struct menu *menu, uint32_t index, char *out,
                      uint32_t out_size) {
    switch (menu->screen) {
    case MENU_ROOT:
        strncpy(out, index < ROOT_COUNT ? ROOT_ITEMS[index] : "", out_size - 1);
        out[out_size - 1] = '\0';
        return;
    case MENU_TRACKS:
        snprintf(out, out_size, "TRACK %lu", (unsigned long)(index + 1));
        return;
    default: {
        const char *directory = screen_directory(menu);
        if (directory == NULL || !nth_file(directory, index, out, out_size)) {
            out[0] = '\0';
        }
        return;
    }
    }
}

static const char *title_for(const struct menu *menu) {
    switch (menu->screen) {
    case MENU_ROOT:
        return "MENU";
    case MENU_SONGS:
        return "LOAD SONG";
    case MENU_TRACKS:
        return "WHICH TRACK";
    case MENU_SAMPLES:
        return "SAMPLE";
    default:
        return "";
    }
}

void menu_draw(const struct menu *menu, uint8_t selected_track) {
    (void)selected_track;
    struct menu *mutable = (struct menu *)menu;

    /* Keep the selection in the middle where it can be, so turning the knob
     * moves the list rather than only the highlight. */
    uint32_t first = 0;
    if (menu->count > MENU_VISIBLE) {
        if (menu->index >= MENU_VISIBLE - 1) {
            first = menu->index - (MENU_VISIBLE - 2);
        }
        if (first + MENU_VISIBLE > menu->count) {
            first = menu->count - MENU_VISIBLE;
        }
    }

    if (!menu->window_valid || menu->window_first != first) {
        for (uint32_t row = 0; row < MENU_VISIBLE; row++) {
            label_for(menu, first + row, mutable->visible[row],
                      FAT_NAME_MAX);
            if (first + row >= menu->count) {
                mutable->visible[row][0] = '\0';
            }
        }
        mutable->window_first = first;
        mutable->window_valid = true;
    }

    display_clear();

    /* The title on an inverted bar, so a screen is identifiable at a glance
     * without reading it. */
    display_fill_rect(0, 0, OLED_WIDTH, FONT_HEIGHT, true);
    display_text(1, 0, title_for(menu), false);

    for (uint32_t row = 0; row < MENU_VISIBLE; row++) {
        uint32_t y = FONT_HEIGHT + row * FONT_HEIGHT;
        if (y + FONT_HEIGHT > OLED_HEIGHT) {
            break;
        }
        bool chosen = (first + row) == menu->index;
        if (chosen) {
            display_fill_rect(0, y, OLED_WIDTH, FONT_HEIGHT, true);
        }
        display_text(1, y, menu->visible[row], !chosen);
    }
}
