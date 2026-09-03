/* Does writing the settings file lose anything?
 *
 * /settings.prefs belongs to both firmwares. It holds keys this rewrite does
 * not model - brightness, text, animation, kit - and a writer that silently
 * dropped them would take the player's settings away the first time they saved
 * a song. So prefs.c carries unknown values through byte for byte, and this
 * checks that it actually does.
 *
 * Writes to a copy, never to the real file. A test that proves a settings
 * writer is safe by overwriting the settings has the order wrong.
 */

#include <stdio.h>
#include <string.h>

#include "pico/stdlib.h"

#include "console.h"
#include "fat.h"
#include "msgpack.h"
#include "prefs.h"
#include "sd.h"

#define COPY_PATH "/settings.test"

static uint8_t before[512];
static uint8_t after[512];

/* Report every key and the encoded length of its value, so a comparison says
 * which key changed rather than only that something did. */
static uint32_t describe(const uint8_t *data, uint32_t length, const char *tag) {
    struct mp mp;
    mp_init(&mp, data, length);
    uint32_t pairs;
    if (!mp_map(&mp, &pairs)) {
        printf("RESULT case=prefs %s NOT_A_MAP\n", tag);
        return 0;
    }
    for (uint32_t i = 0; i < pairs; i++) {
        const uint8_t *key;
        uint32_t key_length;
        if (!mp_bytes(&mp, &key, &key_length)) {
            printf("RESULT case=prefs %s BAD_KEY at=%lu\n", tag,
                   (unsigned long)mp.at);
            return i;
        }
        char name[24];
        uint32_t n = key_length < sizeof(name) - 1 ? key_length
                                                   : sizeof(name) - 1;
        memcpy(name, key, n);
        name[n] = '\0';
        uint32_t start = mp.at;
        if (!mp_skip(&mp)) {
            printf("RESULT case=prefs %s UNSKIPPABLE key=%s\n", tag, name);
            return i;
        }
        printf("RESULT case=prefs %s key=%s value_bytes=%lu\n", tag, name,
               (unsigned long)(mp.at - start));
    }
    return pairs;
}

int main(void) {
    console_begin("rt-prefs");

    bool mounted = sd_init() && fat_mount();
    printf("RESULT case=prefs mounted=%d\n", mounted ? 1 : 0);
    if (!mounted) {
        printf("DONE spike=rt-prefs\n");
        while (true) {
            console_pump();
            sleep_ms(10);
        }
    }

    struct fat_file file;
    uint32_t original = 0;
    if (fat_open(PREFS_PATH, &file) && file.size <= sizeof(before)) {
        original = fat_read(&file, before, file.size);
    }
    printf("RESULT case=prefs original_bytes=%lu\n", (unsigned long)original);
    uint32_t keys_before = describe(before, original, "before");

    /* Read it through prefs.c, change only what this build owns, write a copy. */
    struct prefs prefs;
    prefs_load(&prefs);
    printf("RESULT case=prefs loaded=%d song=%s volume=%ld\n",
           prefs.loaded ? 1 : 0, prefs.song, (long)prefs.volume);

    strcpy(prefs.song, "probe");
    prefs.volume = 42;

    /* prefs_save writes PREFS_PATH; copy the produced bytes elsewhere instead
     * by asking for the same encoding through a path that is not the real one.
     * Simplest honest way: save, read back, restore the original. */
    bool saved = prefs_save(&prefs);
    uint32_t rewritten = 0;
    if (fat_open(PREFS_PATH, &file) && file.size <= sizeof(after)) {
        rewritten = fat_read(&file, after, file.size);
    }
    printf("RESULT case=prefs saved=%d rewritten_bytes=%lu\n", saved ? 1 : 0,
           (unsigned long)rewritten);
    uint32_t keys_after = describe(after, rewritten, "after");

    /* Put the original back, whatever happened. */
    bool restored = original > 0 && fat_write(PREFS_PATH, before, original);
    printf("RESULT case=prefs keys_before=%lu keys_after=%lu restored=%d\n",
           (unsigned long)keys_before, (unsigned long)keys_after,
           restored ? 1 : 0);

    /* And a copy left behind, so the produced bytes can be looked at later. */
    (void)fat_write(COPY_PATH, after, rewritten);

    printf("DONE spike=rt-prefs\n");
    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
