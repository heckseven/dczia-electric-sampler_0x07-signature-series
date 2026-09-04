/* What the badge remembers between sessions.
 *
 * The same file the Python writes - /settings.prefs on the card, a msgpack map
 * with the keys prefs.py uses: "song", "volume", "kit", "brightness", "text",
 * "animation". Sharing it rather than starting a second settings file means the
 * two firmwares agree about which song was last open, which is the whole point
 * of remembering it.
 *
 * Keys this build does not understand are carried through untouched on a write.
 * Dropping "animation" because the rewrite has no animations yet would lose the
 * player's setting the first time they saved anything.
 */

#ifndef PREFS_H
#define PREFS_H

#include <stdbool.h>
#include <stdint.h>

#define PREFS_PATH "/settings.prefs"
#define PREFS_NAME_MAX 32

/* Brightness, as a percentage, matching prefs.py.
 *
 * The ceiling is a power limit and not a taste one: ten pixels at full white
 * would draw about 600 mA from a rail whose only source is the Pico's own
 * regulator, which is also running the card and the amplifier. Fifty keeps the
 * worst case near 300 mA. prefs.py's comment ends "do not raise it without
 * measuring", and that applies here too. */
#define PREFS_BRIGHTNESS_DEFAULT 10
#define PREFS_BRIGHTNESS_MIN 1
#define PREFS_BRIGHTNESS_MAX 50

/* -1 in the Python, meaning "no volume remembered". */
#define PREFS_NO_VOLUME -1

struct prefs {
    char song[PREFS_NAME_MAX];
    /* Panel brightness as a percentage, shared with the Python's "brightness"
     * key so both firmwares mean the same thing by it. Capped at 50 - see the
     * note beside the list in menu.c, and MAX_BRIGHTNESS in prefs.py. */
    uint8_t brightness;
    int32_t volume; /* encoder position, or PREFS_NO_VOLUME */
    bool loaded;
};

void prefs_load(struct prefs *prefs);
bool prefs_save(const struct prefs *prefs);

/* The format on its own, with no filesystem attached.
 *
 * Split out for the same reason the song format was: this file is shared with
 * the Python firmware, and the part that can silently destroy the player's
 * settings - a key dropped on write, a map header that disagrees with what
 * follows it - is arithmetic that needs no card to reproduce. */
void prefs_decode(const uint8_t *data, uint32_t length, struct prefs *prefs);
bool prefs_encode(uint8_t *out, uint32_t capacity, const struct prefs *prefs,
                  uint32_t *length_out);

#endif /* PREFS_H */
