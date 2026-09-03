/* Read-only, bounded, and noisy: find out what state the card is actually in.
 *
 * Written because the pull test stopped producing output, and the audit it runs
 * at boot is the obvious suspect: it reads each file until fat_read returns
 * zero, which never happens if a cluster chain loops back on itself. A torn
 * write can produce exactly that, and a checker that hangs on the damage it was
 * meant to report is worse than no checker.
 *
 * So: every read is capped, every file is announced before it is read rather
 * than after, and nothing is written. If this hangs, the last name printed is
 * the file that did it.
 */

#include <stdio.h>
#include <string.h>

#include "pico/stdlib.h"

#include "console.h"
#include "fat.h"
#include "sd.h"

static uint32_t seen[128];
static uint32_t seen_count;

static void scan(const char *directory) {
    char name[FAT_NAME_MAX];
    bool is_dir = false;
    uint32_t size = 0;

    for (uint32_t i = 0; i < 80; i++) {
        if (!fat_list(directory, i, name, sizeof(name), &is_dir, &size)) {
            break;
        }
        if (is_dir) {
            continue;
        }

        char path[FAT_NAME_MAX + 24];
        strcpy(path, directory);
        strcat(path, "/");
        strncat(path, name, FAT_NAME_MAX - 1);

        /* Announced first, so a hang names its own cause. */
        printf("RESULT case=scan reading=%s declared=%lu\n", name,
               (unsigned long)size);
        console_pump();

        struct fat_file file;
        if (!fat_open(path, &file)) {
            printf("RESULT case=scan file=%s OPEN_FAILED\n", name);
            continue;
        }

        /* Capped at twice the declared size. A chain that loops would otherwise
         * read for ever, and the cap turns that from a hang into a finding. */
        uint32_t cap = size * 2 + 1024;
        uint32_t total = 0, got;
        uint8_t chunk[128];
        bool overran = false;
        while ((got = fat_read(&file, chunk, sizeof(chunk))) != 0) {
            total += got;
            if (total > cap) {
                overran = true;
                break;
            }
            if ((total & 0x3FFF) == 0) {
                console_pump();
            }
        }

        bool duplicate = false;
        if (file.first_cluster >= 2 && seen_count < count_of(seen)) {
            for (uint32_t j = 0; j < seen_count; j++) {
                if (seen[j] == file.first_cluster) {
                    duplicate = true;
                }
            }
            seen[seen_count++] = file.first_cluster;
        }

        printf("RESULT case=scan file=%s declared=%lu read=%lu cluster=%lu "
               "%s%s%s\n",
               name, (unsigned long)size, (unsigned long)total,
               (unsigned long)file.first_cluster,
               total == size ? "ok" : "SIZE_MISMATCH",
               overran ? " OVERRAN" : "", duplicate ? " CROSSLINK" : "");
        console_pump();
    }
}

int main(void) {
    console_begin("rt-scan");

    bool card = sd_init();
    bool mounted = card && fat_mount();
    printf("RESULT case=scan card=%d mounted=%d\n", card ? 1 : 0,
           mounted ? 1 : 0);

    if (mounted) {
        scan("/songs");
        scan("/samples");
        printf("RESULT case=scan files=%lu\n", (unsigned long)seen_count);
    }

    printf("DONE spike=rt-scan\n");
    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
