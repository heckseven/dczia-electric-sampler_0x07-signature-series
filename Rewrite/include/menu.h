/* A menu, which is mostly a way of not needing a keyboard.
 *
 * The badge has twelve keys and two knobs, so anything involving a name is
 * chosen rather than typed. That shapes the whole design: every screen is a
 * list, the Select knob moves through it, and Play takes the item.
 *
 * One screen is not a list. Saving under a new name genuinely needs characters
 * that are not already on the card, and the two knobs turn out to be enough
 * without a character-picker list: Select picks the letter, Volume moves the
 * cursor along the name. Two knobs, two axes, and Play and Function keep
 * meaning what they mean everywhere else.
 *
 * Select picks the letter rather than moving the cursor because that is what
 * Select does on every other screen here, and what engine/naming.py's single
 * knob does too. The cursor is the addition; it goes on the knob that was
 * otherwise idle.
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
    MENU_NAME,    /* type a name for the song being saved */
    MENU_ANIM,    /* pick what the light strip does when nothing is happening */

    /* Settings. Each one is its own list of choices, which is the same shape
     * every other screen here already has - a setting with three or six legal
     * values is a list, and giving it a bespoke editor would be a second
     * interaction to learn for no gain. */
    MENU_SETTINGS,
    MENU_DIVISION,   /* how long a step is */
    MENU_LENGTH,     /* pattern length, applied to every track */
    MENU_BRIGHTNESS, /* the light strip */
    MENU_SYNC,       /* pulses per quarter note on the sync jack */
    MENU_DELETE,     /* remove a saved song */
};

/* Long enough for a name worth typing on two knobs, and short enough that the
 * whole thing fits one row of 6-pixel type across a 128-pixel panel. */
#define MENU_NAME_MAX 16

/* What the main loop is being asked to do. The menu decides what was chosen;
 * it does not load anything itself, because loading a kit means touching the
 * audio arena and that belongs to whoever owns it. */
enum menu_action {
    MENU_ACTION_NONE = 0,
    MENU_ACTION_LOAD_SONG,
    MENU_ACTION_SAVE_SONG,
    MENU_ACTION_SET_SAMPLE,
    MENU_ACTION_SET_ANIM,
    MENU_ACTION_SET_DIVISION,
    MENU_ACTION_SET_LENGTH,
    MENU_ACTION_SET_BRIGHTNESS,
    MENU_ACTION_SET_SYNC,
    MENU_ACTION_DELETE_SONG,
};

/* How deep the screens nest. Four is more than the current tree needs and is
 * there so adding a level is a data change rather than a structural one. */
#define MENU_DEPTH 4

/* What the instrument is currently set to.
 *
 * The menu draws the current value beside each setting, so a glance at the
 * list answers "what is it now" as well as "what could it be" - which is the
 * whole reason to have a settings screen rather than a set of gestures.
 *
 * Passed in rather than read: this module has no song, no transport and no
 * light strip, and giving it one so it could label a row would be a poor
 * trade for a struct of four bytes. */
struct menu_context {
    uint8_t division;
    uint8_t length;
    uint8_t brightness_pct;
    uint8_t sync_ppqn;
};


struct menu_level {
    enum menu_screen screen;
    uint32_t index; /* selected item, remembered while deeper in */
    uint32_t count;
};

struct menu {
    /* A stack, so going back returns to where you were rather than to the top.
     * The first version hard-coded which screen each one returned to, which
     * works for a tree of three and stops working the moment it is four. */
    struct menu_level stack[MENU_DEPTH];
    uint32_t depth; /* 0 means closed */

    /* The window actually on screen, re-read only when it moves - a list
     * rescanned every frame would re-walk the directory thirty times a second
     * to show the same three names. */
    char visible[MENU_VISIBLE][FAT_NAME_MAX];
    uint32_t window_first;
    bool window_valid;

    uint8_t track; /* which track a sample is being chosen for */
    uint8_t anim;  /* which animation was picked */
    /* What the chosen row meant, for the settings that are a plain number.
     * One field rather than one per setting: only ever one is being answered,
     * and the action says which question it was. */
    uint32_t value;

    struct menu_context context;

    /* The name being typed, and where the cursor sits in it. Held padded with
     * spaces to its full width rather than NUL-terminated early: the cursor has
     * to be able to move past the end of what has been typed so far, and a
     * ragged buffer makes that a special case at every turn of the knob. */
    char name[MENU_NAME_MAX + 1];
    uint8_t cursor;

    /* Filled in when an action is returned. */
    char chosen[FAT_NAME_MAX];
};

void menu_set_context(struct menu *menu, const struct menu_context *context);

void menu_open(struct menu *menu);
void menu_close(struct menu *menu);
bool menu_is_open(const struct menu *menu);

void menu_turn(struct menu *menu, int32_t delta);

/* The other knob: moves the cursor on the name screen, and nothing anywhere
 * else - on a list there is no second axis to mean anything, and giving it one
 * would be a control the player has to discover is inert. */
void menu_turn_volume(struct menu *menu, int32_t delta);

/* Seed the name screen, so re-saving an already-named song is a click rather
 * than sixteen turns of a knob. Call it after menu_open, which clears the name
 * along with everything else. */
void menu_set_name(struct menu *menu, const char *name);

/* Enter: take the selected item. Play, on the badge - Function is back, which
 * is the arrangement the player asked for and the one the rest of the firmware
 * already implies, since Function is the modifier everywhere else. */
enum menu_action menu_enter(struct menu *menu);

/* Back one screen, or out of the menu from the root. */
void menu_back(struct menu *menu);

/* Draw into the framebuffer. The caller flushes. */
void menu_draw(const struct menu *menu);

#endif /* MENU_H */
