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

static enum menu_screen current(const struct menu *menu) {
    return menu->depth ? menu->stack[menu->depth - 1].screen : MENU_CLOSED;
}

static struct menu_level *top(struct menu *menu) {
    return menu->depth ? &menu->stack[menu->depth - 1] : NULL;
}

static const char *screen_directory(enum menu_screen screen) {
    switch (screen) {
    case MENU_SONGS:
        return SONG_DIR;
    case MENU_SAMPLES:
        return SAMPLE_DIR;
    default:
        return NULL;
    }
}

static uint32_t items_on(enum menu_screen screen) {
    const char *directory = screen_directory(screen);
    if (directory != NULL) {
        return count_files(directory);
    }
    switch (screen) {
    case MENU_ROOT:
        return ROOT_COUNT;
    case MENU_TRACKS:
        return TRACK_COUNT;
    default:
        return 0;
    }
}

/* Counted on entry rather than every frame, but recounted whenever a screen is
 * entered - a song saved between one look and the next should appear. */
static void push(struct menu *menu, enum menu_screen screen) {
    if (menu->depth >= MENU_DEPTH) {
        return;
    }
    struct menu_level *level = &menu->stack[menu->depth++];
    level->screen = screen;
    level->index = 0;
    level->count = items_on(screen);
    menu->window_valid = false;
}

void menu_open(struct menu *menu) {
    memset(menu, 0, sizeof(*menu));
    push(menu, MENU_ROOT);
}

void menu_close(struct menu *menu) {
    menu->depth = 0;
    menu->window_valid = false;
}

bool menu_is_open(const struct menu *menu) {
    return menu->depth != 0;
}

void menu_turn(struct menu *menu, int32_t delta) {
    struct menu_level *level = top(menu);
    if (level == NULL || level->count == 0) {
        return;
    }
    int32_t next = (int32_t)level->index + delta;
    /* Stop at the ends rather than wrapping. A list that wraps makes "how far
     * down am I" unanswerable on a three-row window. */
    if (next < 0) {
        next = 0;
    }
    if (next >= (int32_t)level->count) {
        next = (int32_t)level->count - 1;
    }
    if ((uint32_t)next != level->index) {
        level->index = (uint32_t)next;
        menu->window_valid = false;
    }
}

void menu_back(struct menu *menu) {
    if (menu->depth > 1) {
        menu->depth--;
        /* The level below keeps the index it had, so backing out returns to
         * where you were rather than to the top of it. */
        menu->stack[menu->depth - 1].count =
            items_on(menu->stack[menu->depth - 1].screen);
        menu->window_valid = false;
    } else {
        menu_close(menu);
    }
}

enum menu_action menu_enter(struct menu *menu) {
    struct menu_level *level = top(menu);
    if (level == NULL) {
        return MENU_ACTION_NONE;
    }

    switch (level->screen) {
    case MENU_ROOT:
        if (level->index == 0) {
            push(menu, MENU_SONGS);
        } else if (level->index == 1) {
            menu_close(menu);
            return MENU_ACTION_SAVE_SONG;
        } else {
            push(menu, MENU_TRACKS);
        }
        return MENU_ACTION_NONE;

    case MENU_SONGS:
        if (nth_file(SONG_DIR, level->index, menu->chosen,
                     sizeof(menu->chosen))) {
            menu_close(menu);
            return MENU_ACTION_LOAD_SONG;
        }
        return MENU_ACTION_NONE;

    case MENU_TRACKS:
        menu->track = (uint8_t)level->index;
        push(menu, MENU_SAMPLES);
        return MENU_ACTION_NONE;

    case MENU_SAMPLES:
        if (nth_file(SAMPLE_DIR, level->index, menu->chosen,
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

static void label_for(enum menu_screen screen, uint32_t index, char *out,
                      uint32_t out_size) {
    switch (screen) {
    case MENU_ROOT:
        strncpy(out, index < ROOT_COUNT ? ROOT_ITEMS[index] : "", out_size - 1);
        out[out_size - 1] = '\0';
        return;
    case MENU_TRACKS:
        snprintf(out, out_size, "TRACK %lu", (unsigned long)(index + 1));
        return;
    default: {
        const char *directory = screen_directory(screen);
        if (directory == NULL || !nth_file(directory, index, out, out_size)) {
            out[0] = '\0';
        }
        return;
    }
    }
}

static const char *title_for(enum menu_screen screen) {
    switch (screen) {
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

void menu_draw(const struct menu *menu) {
    struct menu *mutable = (struct menu *)menu;
    if (menu->depth == 0) {
        return;
    }
    const struct menu_level *level = &menu->stack[menu->depth - 1];

    /* Keep the selection in the middle where it can be, so turning the knob
     * moves the list rather than only the highlight. */
    uint32_t first = 0;
    if (level->count > MENU_VISIBLE) {
        if (level->index >= MENU_VISIBLE - 1) {
            first = level->index - (MENU_VISIBLE - 2);
        }
        if (first + MENU_VISIBLE > level->count) {
            first = level->count - MENU_VISIBLE;
        }
    }

    if (!menu->window_valid || menu->window_first != first) {
        for (uint32_t row = 0; row < MENU_VISIBLE; row++) {
            if (first + row < level->count) {
                label_for(level->screen, first + row, mutable->visible[row],
                          FAT_NAME_MAX);
            } else {
                mutable->visible[row][0] = '\0';
            }
        }
        mutable->window_first = first;
        mutable->window_valid = true;
    }

    display_clear();

    /* The heading is plain. Inverting it competes with the selected item for
     * the eye, and only one thing on a screen this size should be shouting. */
    display_text(1, 0, title_for(level->screen), true);

    for (uint32_t row = 0; row < MENU_VISIBLE; row++) {
        uint32_t y = FONT_PITCH * (row + 1);
        if (y + FONT_PITCH > OLED_HEIGHT) {
            break;
        }
        bool chosen = (first + row) == level->index;
        if (chosen) {
            display_fill_rect(0, y, OLED_WIDTH, FONT_PITCH, true);
        }
        display_text(1, y, menu->visible[row], !chosen);
    }
}
