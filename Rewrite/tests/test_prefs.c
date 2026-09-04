/* The settings file, round-tripped without a card.
 *
 * This file is shared with the CircuitPython firmware, and the way it breaks is
 * not a crash. The Python reads every key with a default, so a key this writer
 * drops does not fail to load - it resets, silently, and looks exactly like
 * nothing happened. The song format had two fields destroyed that way before a
 * round-trip test found them.
 */

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "fat.h"
#include "prefs.h"

/* --- the filesystem, stubbed ---------------------------------------------- */

bool fat_open(const char *path, struct fat_file *file) {
    (void)path;
    (void)file;
    return false;
}
uint32_t fat_read(struct fat_file *file, void *buffer, uint32_t length) {
    (void)file;
    (void)buffer;
    (void)length;
    return 0;
}
bool fat_write(const char *path, const uint8_t *data, uint32_t length) {
    (void)path;
    (void)data;
    (void)length;
    return true;
}

/* --- harness --------------------------------------------------------------- */

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

/* Is this byte sequence anywhere in the buffer? Bounded by the needle's own
 * length, so a value at the very end is still found. */
static bool contains(const uint8_t *haystack, uint32_t length,
                     const char *needle) {
    uint32_t n = (uint32_t)strlen(needle);
    if (n > length) {
        return false;
    }
    for (uint32_t i = 0; i + n <= length; i++) {
        if (memcmp(&haystack[i], needle, n) == 0) {
            return true;
        }
    }
    return false;
}

static void test_round_trip(void) {
    struct prefs out;
    prefs_decode(NULL, 0, &out);
    strncpy(out.song, "MY PATTERN", PREFS_NAME_MAX - 1);
    out.volume = 1234;
    out.brightness = 25;

    uint8_t buffer[512];
    uint32_t length = 0;
    check(prefs_encode(buffer, sizeof(buffer), &out, &length), "it encodes");

    struct prefs back;
    prefs_decode(buffer, length, &back);
    check(strcmp(back.song, "MY PATTERN") == 0, "the song name survives");
    check(back.volume == 1234, "the volume survives");
    check(back.brightness == 25, "the brightness survives");
}

static void test_defaults_when_there_is_nothing_to_read(void) {
    /* A fresh card, or a file too short to be one. The badge has to come up
     * playable rather than silent and dark. */
    struct prefs prefs;
    prefs_decode(NULL, 0, &prefs);
    check(prefs.volume == PREFS_NO_VOLUME, "no volume is remembered");
    check(prefs.brightness == PREFS_BRIGHTNESS_DEFAULT,
          "brightness falls back to the default rather than to zero");
    check(prefs.song[0] == '\0', "and no song is named");
}

static void test_brightness_is_clamped_on_the_way_in(void) {
    /* The ceiling is a power limit: ten pixels at full white would pull about
     * 600 mA from a rail that is also running the card and the amplifier. A
     * file edited by hand, or written by something with a higher ceiling, must
     * not be able to ask for more than the board can deliver. */
    static const uint8_t too_bright[] = {
        0x81, 0xAA, 'b', 'r', 'i', 'g', 'h', 't', 'n', 'e', 's', 's', 0x64,
    }; /* {"brightness": 100} */
    struct prefs prefs;
    prefs_decode(too_bright, sizeof(too_bright), &prefs);
    check(prefs.brightness == PREFS_BRIGHTNESS_MAX,
          "a brightness past the ceiling is brought down to it");

    static const uint8_t too_dark[] = {
        0x81, 0xAA, 'b', 'r', 'i', 'g', 'h', 't', 'n', 'e', 's', 's', 0x00,
    }; /* {"brightness": 0} */
    prefs_decode(too_dark, sizeof(too_dark), &prefs);
    check(prefs.brightness == PREFS_BRIGHTNESS_MIN,
          "and zero comes up to the minimum rather than going dark");
}

static void test_unknown_keys_are_carried_through(void) {
    /* The Python writes keys this firmware has no use for - "kit", "text" and
     * whatever comes next. Saving from the badge must give them back exactly,
     * or the player loses settings they made on the other firmware and nothing
     * anywhere says so.
     *
     * The map header is the sharp edge: it counts pairs, so adding a key here
     * without changing the count writes a file whose header disagrees with its
     * body, and every reader of it desynchronises. */
    static const uint8_t from_python[] = {
        0x83,
        0xA4, 's', 'o', 'n', 'g', 0xA4, 'B', 'E', 'A', 'T',
        0xA3, 'k', 'i', 't', 0xA5, 'B', 'A', 'S', 'I', 'C',
        0xA4, 't', 'e', 'x', 't', 0xA2, 'H', 'I',
    }; /* {"song": "BEAT", "kit": "BASIC", "text": "HI"} */

    struct prefs prefs;
    prefs_decode(from_python, sizeof(from_python), &prefs);
    check(strcmp(prefs.song, "BEAT") == 0, "the known key is read");

    uint8_t buffer[512];
    uint32_t length = 0;
    check(prefs_encode(buffer, sizeof(buffer), &prefs, &length), "and rewritten");

    /* Both unknown keys and both their values come back.
     *
     * The search is bounded per needle rather than by the longest of them: a
     * single `i + 5 <= length` stops five bytes early, and the last thing
     * written is a two-byte value - so the test would miss exactly the key
     * most likely to be truncated by a real bug. */
    bool kit = contains(buffer, length, "kit");
    bool basic = contains(buffer, length, "BASIC");
    bool text = contains(buffer, length, "text");
    bool hi = contains(buffer, length, "HI");
    check(kit && basic, "an unknown key keeps its value");
    check(text && hi, "and so does the one after it, right at the end");

    /* And the header still counts what is actually there: three known keys
     * plus the two carried. A wrong count here is a file no reader recovers
     * from, including the Python's. */
    check((buffer[0] & 0xF0) == 0x80, "the document is still a fixmap");
    check((buffer[0] & 0x0F) == 5, "counting all five pairs");

    /* Re-reading what was written must give the same thing back, which is the
     * property that actually matters. */
    struct prefs again;
    prefs_decode(buffer, length, &again);
    check(strcmp(again.song, "BEAT") == 0, "a second trip changes nothing");
}

int main(void) {
    test_round_trip();
    test_defaults_when_there_is_nothing_to_read();
    test_brightness_is_clamped_on_the_way_in();
    test_unknown_keys_are_carried_through();

    if (failures == 0) {
        printf("ok - all prefs tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
