/* See menu.h. */

#include <stdio.h>
#include <string.h>

#include "display.h"
#include "font.h"
#include "menu.h"
#include "songfile.h"

#define SAMPLE_DIR "/samples"

/* What the Volume knob cycles through, in the order it cycles.
 *
 * Space first so a fresh name reads as empty rather than as a row of As, then
 * letters, then digits, then the two punctuation marks that survive an 8.3
 * filesystem without being mangled. No lower case: FAT stores a short name
 * upper-cased anyway, and offering a distinction the card does not keep would
 * produce two names that look different and are the same file. */
static const char CHARSET[] = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_";
#define CHARSET_COUNT (sizeof(CHARSET) - 1)

static uint32_t charset_index(char c) {
    for (uint32_t i = 0; i < CHARSET_COUNT; i++) {
        if (CHARSET[i] == c) {
            return i;
        }
    }
    return 0; /* anything unrepresentable reads as a space */
}

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
    /* Including the name, which is why menu_set_name has to come after this
     * and not before. Opening on a name left over from last time would offer
     * to overwrite a file the player is no longer looking at. */
    memset(menu->name, ' ', MENU_NAME_MAX);
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
    if (level == NULL) {
        return;
    }
    if (level->screen == MENU_NAME) {
        int32_t next = (int32_t)menu->cursor + delta;
        if (next < 0) {
            next = 0;
        }
        if (next >= MENU_NAME_MAX) {
            next = MENU_NAME_MAX - 1;
        }
        menu->cursor = (uint8_t)next;
        return;
    }
    if (level->count == 0) {
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

void menu_turn_volume(struct menu *menu, int32_t delta) {
    struct menu_level *level = top(menu);
    if (level == NULL || level->screen != MENU_NAME) {
        return;
    }
    /* Wraps, unlike a list. There is no "how far through the alphabet am I"
     * to lose, and stopping at Z would mean turning back thirty-eight clicks
     * to reach a space. */
    int32_t index = (int32_t)charset_index(menu->name[menu->cursor]) + delta;
    index %= (int32_t)CHARSET_COUNT;
    if (index < 0) {
        index += (int32_t)CHARSET_COUNT;
    }
    menu->name[menu->cursor] = CHARSET[index];
}

void menu_set_name(struct menu *menu, const char *name) {
    memset(menu->name, ' ', MENU_NAME_MAX);
    menu->name[MENU_NAME_MAX] = '\0';
    uint32_t i = 0;
    for (; name != NULL && name[i] != '\0' && i < MENU_NAME_MAX; i++) {
        char c = name[i];
        if (c >= 'a' && c <= 'z') {
            c = (char)(c - 'a' + 'A');
        }
        /* Anything the knob cannot reach becomes a space, so every name on
         * screen is one the player could have typed and could type again. */
        menu->name[i] = (charset_index(c) == 0 && c != ' ') ? ' ' : c;
    }
    menu->cursor = (uint8_t)(i < MENU_NAME_MAX ? i : MENU_NAME_MAX - 1);
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
            push(menu, MENU_NAME);
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

    case MENU_NAME: {
        /* Trim the padding back off. Leading spaces go too: a name that begins
         * with one is a file nobody can pick out of a list later. */
        uint32_t first = 0;
        while (first < MENU_NAME_MAX && menu->name[first] == ' ') {
            first++;
        }
        uint32_t last = MENU_NAME_MAX;
        while (last > first && menu->name[last - 1] == ' ') {
            last--;
        }
        if (last == first) {
            /* Nothing typed. Refused rather than saved as an empty name, which
             * would produce a file the song list cannot show. */
            return MENU_ACTION_NONE;
        }
        uint32_t length = last - first;
        memcpy(menu->chosen, &menu->name[first], length);
        menu->chosen[length] = '\0';
        menu_close(menu);
        return MENU_ACTION_SAVE_SONG;
    }

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

/* The name screen, which is the one screen that is not a list.
 *
 * Four rows: the heading, the name, the cursor, and what the two buttons do.
 * The cursor is a rule under the character rather than an inverted block,
 * because the selected item on every other screen is an inverted block and two
 * different things should not look the same. */
static void draw_name(const struct menu *menu) {
    display_clear();
    display_text(1, 0, "NAME", true);

    /* Centred, so a short name does not sit against the left edge looking like
     * it was left unfinished. Sixteen characters of 6-pixel type is 96 of the
     * panel's 128, which leaves 16 either side. */
    const uint32_t left = (OLED_WIDTH - MENU_NAME_MAX * FONT_WIDTH) / 2;
    display_text(left, FONT_PITCH, menu->name, true);
    display_fill_rect(left + menu->cursor * FONT_WIDTH, FONT_PITCH * 2 + 1,
                      FONT_WIDTH, 1, true);

    display_text(1, FONT_PITCH * 3, "PLAY SAVE  FN BACK", true);
}

void menu_draw(const struct menu *menu) {
    struct menu *mutable = (struct menu *)menu;
    if (menu->depth == 0) {
        return;
    }
    const struct menu_level *level = &menu->stack[menu->depth - 1];

    if (level->screen == MENU_NAME) {
        draw_name(menu);
        return;
    }

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
