/* One-off: remove the duplicate CSAVE.SON entries an earlier version of the
 * write path created.
 *
 * It truncated "csave.song" to the 8.3 name "CSAVE.SON", then looked for the
 * untruncated name when deciding whether the file already existed - so the
 * second save did not find the first and made another entry beside it. Two
 * directory entries with the same short name is not a filesystem any checker
 * will call clean, and it is on the player's card, so it gets removed here
 * rather than left for them to find.
 */

#include <stdio.h>

#include "pico/stdlib.h"

#include "console.h"
#include "fat.h"
#include "sd.h"

int main(void) {
    console_begin("rt-clean");

    bool mounted = sd_init() && fat_mount();
    printf("RESULT case=clean mounted=%d\n", mounted ? 1 : 0);
    if (mounted) {
        /* Delete until there is nothing left of that name: there are two, and
         * each call removes one. */
        for (uint32_t i = 0; i < 8; i++) {
            if (!fat_delete("/songs/CSAVE.SON")) {
                break;
            }
            printf("RESULT case=clean removed=CSAVE.SON\n");
        }

        char name[FAT_NAME_MAX];
        bool is_dir = false;
        uint32_t size = 0;
        for (uint32_t i = 0; i < 16; i++) {
            if (!fat_list("/songs", i, name, sizeof(name), &is_dir, &size)) {
                break;
            }
            printf("RESULT case=clean remains=%s bytes=%lu\n", name,
                   (unsigned long)size);
        }
    }

    printf("DONE spike=rt-clean\n");
    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
