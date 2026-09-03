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

/* -1 in the Python, meaning "no volume remembered". */
#define PREFS_NO_VOLUME -1

struct prefs {
    char song[PREFS_NAME_MAX];
    int32_t volume; /* encoder position, or PREFS_NO_VOLUME */
    bool loaded;
};

void prefs_load(struct prefs *prefs);
bool prefs_save(const struct prefs *prefs);

#endif /* PREFS_H */
