/* A menu, which is mostly a way of not needing a keyboard.
 *
 * The badge has twelve keys and two knobs, so anything involving a name has to
 * be chosen rather than typed. That shapes the whole design: every screen is a
 * list, the Select knob moves through it, and clicking takes the item. There is
 * no text entry anywhere and there does not need to be.
 *
 * Lists come from the card as they are drawn rather than being read into
 * memory. /samples holds 85 files and could hold hundreds; a menu that loaded
 * them all to show three would be spending the sample arena on filenames.
 */

#ifndef MENU_H
#define MENU_H

#include <stdbool.h>
#include <stdint.h>

#include "fat.h"

/* Three rows of items under a title, on a four-row display. */
#define MENU_VISIBLE 3

enum menu_screen {
    MENU_CLOSED = 0,
    MENU_ROOT,
    MENU_SONGS,   /* pick a song to load */
    MENU_TRACKS,  /* pick which track to give a sample to */
    MENU_SAMPLES, /* pick the sample */
};

/* What the main loop is being asked to do. The menu decides what was chosen;
 * it does not load anything itself, because loading a kit means touching the
 * audio arena and that belongs to whoever owns it. */
enum menu_action {
    MENU_ACTION_NONE = 0,
    MENU_ACTION_LOAD_SONG,
    MENU_ACTION_SAVE_SONG,
    MENU_ACTION_SET_SAMPLE,
    MENU_ACTION_CLOSED,
};

struct menu {
    enum menu_screen screen;
    uint32_t index; /* selected item */
    uint32_t count; /* items on this screen */

    /* The window actually on screen, re-read only when it moves - a list
     * rescanned every frame would re-walk the directory thirty times a second
     * to show the same three names. */
    char visible[MENU_VISIBLE][FAT_NAME_MAX];
    uint32_t window_first;
    bool window_valid;

    uint8_t track; /* which track a sample is being chosen for */

    /* Filled in when an action is returned. */
    char chosen[FAT_NAME_MAX];
};

void menu_open(struct menu *menu);
void menu_close(struct menu *menu);
bool menu_is_open(const struct menu *menu);

void menu_turn(struct menu *menu, int32_t delta);

/* Click the Select knob. Returns what the caller should do about it. */
enum menu_action menu_click(struct menu *menu);

/* Back one screen, or out of the menu from the root. */
void menu_back(struct menu *menu);

/* Draw into the framebuffer. The caller flushes. */
void menu_draw(const struct menu *menu, uint8_t selected_track);

#endif /* MENU_H */
