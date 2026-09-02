/* See fat.h. Read-only FAT32, long names included. */

#include <string.h>

#include "fat.h"
#include "sd.h"

/* One sector of cache. Directory scanning and FAT-chain walking both re-read
 * the same sector repeatedly, and at 476 us a read that adds up fast. One
 * sector is enough because neither ever interleaves two. */
static uint8_t cache[SD_BLOCK];
static uint32_t cached_lba = 0xFFFFFFFFu;

static struct {
    uint32_t partition_lba;
    uint32_t fat_lba;
    uint32_t data_lba;
    uint32_t sectors_per_cluster;
    uint32_t root_cluster;
    bool mounted;
} volume;

static bool read_cached(uint32_t lba) {
    if (lba == cached_lba) {
        return true;
    }
    if (!sd_read(lba, cache)) {
        cached_lba = 0xFFFFFFFFu;
        return false;
    }
    cached_lba = lba;
    return true;
}

static uint16_t le16(const uint8_t *p) {
    return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint32_t cluster_lba(uint32_t cluster) {
    return volume.data_lba + (cluster - 2) * volume.sectors_per_cluster;
}

/* Next cluster in the chain, or >= 0x0FFFFFF8 at the end. */
static uint32_t fat_next(uint32_t cluster) {
    uint32_t offset = cluster * 4;
    uint32_t lba = volume.fat_lba + offset / SD_BLOCK;
    if (!read_cached(lba)) {
        return 0x0FFFFFFFu;
    }
    return le32(&cache[offset % SD_BLOCK]) & 0x0FFFFFFFu;
}

bool fat_mount(void) {
    volume.mounted = false;
    cached_lba = 0xFFFFFFFFu;

    if (!read_cached(0)) {
        return false;
    }

    /* An MBR at block 0, or a filesystem there directly. Cards formatted by a
     * camera or a phone can be either, and the difference is one field. */
    uint32_t start = 0;
    if (cache[510] == 0x55 && cache[511] == 0xAA) {
        const uint8_t *entry = &cache[446];
        uint8_t type = entry[4];
        if (type == 0x0B || type == 0x0C || type == 0x0E || type == 0x06) {
            start = le32(&entry[8]);
        }
    }

    if (!read_cached(start)) {
        return false;
    }
    const uint8_t *bpb = cache;

    uint16_t bytes_per_sector = le16(&bpb[11]);
    uint32_t sectors_per_cluster = bpb[13];
    uint16_t reserved = le16(&bpb[14]);
    uint32_t num_fats = bpb[16];
    uint32_t sectors_per_fat = le32(&bpb[36]);
    uint32_t root_cluster = le32(&bpb[44]);

    /* Only 512-byte sectors. Everything else here assumes SD_BLOCK, and a card
     * with 4096-byte sectors would read silently wrong rather than fail. */
    if (bytes_per_sector != SD_BLOCK || sectors_per_cluster == 0 ||
        sectors_per_fat == 0 || root_cluster < 2) {
        return false;
    }

    volume.partition_lba = start;
    volume.fat_lba = start + reserved;
    volume.data_lba = volume.fat_lba + num_fats * sectors_per_fat;
    volume.sectors_per_cluster = sectors_per_cluster;
    volume.root_cluster = root_cluster;
    volume.mounted = true;
    return true;
}

/* --- names ---------------------------------------------------------------- */

static char upper(char c) {
    return (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
}

static bool names_equal(const char *a, const char *b) {
    while (*a && *b) {
        if (upper(*a) != upper(*b)) {
            return false;
        }
        a++;
        b++;
    }
    return *a == *b;
}

/* The 8.3 name, with the padding removed and the dot put back. */
static void short_name(const uint8_t *entry, char *out) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < 8 && entry[i] != ' '; i++) {
        out[n++] = (char)entry[i];
    }
    if (entry[8] != ' ') {
        out[n++] = '.';
        for (uint32_t i = 8; i < 11 && entry[i] != ' '; i++) {
            out[n++] = (char)entry[i];
        }
    }
    out[n] = '\0';
}

/* One long-name fragment carries thirteen UTF-16 code units, at three
 * non-contiguous offsets. Anything outside ASCII is replaced rather than
 * decoded: sample names are ASCII, and a mangled non-ASCII name is better than
 * a reader that pretends to speak UTF-16. */
static void lfn_fragment(const uint8_t *entry, char *out) {
    static const uint8_t offsets[13] = {1,  3,  5,  7,  9,  14, 16,
                                        18, 20, 22, 24, 28, 30};
    for (uint32_t i = 0; i < 13; i++) {
        uint16_t unit = le16(&entry[offsets[i]]);
        out[i] = (unit == 0 || unit == 0xFFFF)  ? '\0'
                 : (unit < 0x80)                ? (char)unit
                                                : '?';
    }
}

/* --- directory scanning --------------------------------------------------- */

struct scan {
    uint32_t cluster;
    uint32_t sector; /* within the cluster */
    uint32_t index;  /* within the sector */
};

static void scan_begin(struct scan *s, uint32_t cluster) {
    s->cluster = cluster;
    s->sector = 0;
    s->index = 0;
}

/* Next real entry. Assembles long names, skips fragments, volume labels and
 * deleted entries. Returns false at the end of the directory. */
static bool scan_next(struct scan *s, char *name, uint32_t name_size,
                      uint8_t *attr_out, uint32_t *cluster_out,
                      uint32_t *size_out) {
    char assembled[FAT_NAME_MAX];
    uint32_t assembled_len = 0;
    bool have_long = false;

    while (s->cluster >= 2 && s->cluster < 0x0FFFFFF8u) {
        if (s->sector >= volume.sectors_per_cluster) {
            s->cluster = fat_next(s->cluster);
            s->sector = 0;
            s->index = 0;
            continue;
        }
        if (!read_cached(cluster_lba(s->cluster) + s->sector)) {
            return false;
        }
        if (s->index >= SD_BLOCK / 32) {
            s->sector++;
            s->index = 0;
            continue;
        }

        const uint8_t *entry = &cache[s->index * 32];
        s->index++;

        if (entry[0] == 0x00) {
            return false; /* no further entries in this directory */
        }
        if (entry[0] == 0xE5) {
            have_long = false;
            assembled_len = 0;
            continue;
        }

        uint8_t attr = entry[11];
        if ((attr & 0x0F) == 0x0F) {
            /* A long-name fragment. Sequence numbers count down as they are
             * stored, so fragment n holds characters at (n-1)*13. */
            uint32_t sequence = entry[0] & 0x1F;
            if (sequence == 0 || sequence * 13 > FAT_NAME_MAX - 1) {
                have_long = false;
                continue;
            }
            char part[13];
            lfn_fragment(entry, part);
            uint32_t base = (sequence - 1) * 13;
            for (uint32_t i = 0; i < 13; i++) {
                assembled[base + i] = part[i];
                if (part[i] == '\0') {
                    break;
                }
            }
            if (entry[0] & 0x40) {
                /* Last fragment stored is the tail of the name, so this is
                 * where the total length becomes known. */
                assembled_len = base;
                for (uint32_t i = 0; i < 13 && part[i]; i++) {
                    assembled_len++;
                }
                assembled[assembled_len] = '\0';
            }
            have_long = true;
            continue;
        }

        if (attr & 0x08) {
            /* Volume label. Not a file, and its name is not a filename. */
            have_long = false;
            assembled_len = 0;
            continue;
        }

        if (have_long && assembled_len > 0) {
            strncpy(name, assembled, name_size - 1);
            name[name_size - 1] = '\0';
        } else {
            char eight_three[13];
            short_name(entry, eight_three);
            strncpy(name, eight_three, name_size - 1);
            name[name_size - 1] = '\0';
        }

        *attr_out = attr;
        *cluster_out = ((uint32_t)le16(&entry[20]) << 16) | le16(&entry[26]);
        *size_out = le32(&entry[28]);
        return true;
    }
    return false;
}

/* --- public --------------------------------------------------------------- */

/* Copy one path component into `out`, returning where the next begins. */
static const char *split(const char *path, char *out, uint32_t out_size) {
    while (*path == '/') {
        path++;
    }
    uint32_t n = 0;
    while (*path && *path != '/' && n < out_size - 1) {
        out[n++] = *path++;
    }
    out[n] = '\0';
    return path;
}

static bool find(const char *path, uint32_t *cluster_out, uint32_t *size_out,
                 uint8_t *attr_out) {
    if (!volume.mounted) {
        return false;
    }
    uint32_t cluster = volume.root_cluster;
    uint32_t size = 0;
    uint8_t attr = 0x10;

    char want[FAT_NAME_MAX];
    const char *rest = path;
    for (;;) {
        rest = split(rest, want, sizeof(want));
        if (want[0] == '\0') {
            break;
        }
        if (!(attr & 0x10)) {
            return false; /* a path component below a plain file */
        }

        struct scan s;
        scan_begin(&s, cluster);
        char name[FAT_NAME_MAX];
        uint8_t entry_attr;
        uint32_t entry_cluster, entry_size;
        bool found = false;
        while (scan_next(&s, name, sizeof(name), &entry_attr, &entry_cluster,
                         &entry_size)) {
            if (names_equal(name, want)) {
                cluster = entry_cluster;
                size = entry_size;
                attr = entry_attr;
                found = true;
                break;
            }
        }
        if (!found) {
            return false;
        }
    }

    *cluster_out = cluster;
    *size_out = size;
    *attr_out = attr;
    return true;
}

bool fat_open(const char *path, struct fat_file *file) {
    uint32_t cluster, size;
    uint8_t attr;
    if (!find(path, &cluster, &size, &attr) || (attr & 0x10)) {
        return false;
    }
    file->first_cluster = cluster;
    file->size = size;
    file->position = 0;
    file->cluster = cluster;
    file->cluster_offset = 0;
    return true;
}

uint32_t fat_read(struct fat_file *file, void *buffer, uint32_t length) {
    uint8_t *out = buffer;
    uint32_t done = 0;
    uint32_t cluster_bytes = volume.sectors_per_cluster * SD_BLOCK;

    while (done < length && file->position < file->size) {
        if (file->cluster < 2 || file->cluster >= 0x0FFFFFF8u) {
            break;
        }
        if (file->cluster_offset >= cluster_bytes) {
            file->cluster = fat_next(file->cluster);
            file->cluster_offset = 0;
            continue;
        }

        uint32_t sector = file->cluster_offset / SD_BLOCK;
        uint32_t within = file->cluster_offset % SD_BLOCK;
        if (!read_cached(cluster_lba(file->cluster) + sector)) {
            break;
        }

        uint32_t chunk = SD_BLOCK - within;
        uint32_t remaining_file = file->size - file->position;
        uint32_t remaining_want = length - done;
        if (chunk > remaining_file) {
            chunk = remaining_file;
        }
        if (chunk > remaining_want) {
            chunk = remaining_want;
        }

        memcpy(out + done, &cache[within], chunk);
        done += chunk;
        file->position += chunk;
        file->cluster_offset += chunk;
    }
    return done;
}

bool fat_list(const char *path, uint32_t index, char *name_out,
              uint32_t name_size, bool *is_dir, uint32_t *size_out) {
    uint32_t cluster, size;
    uint8_t attr;
    if (!find(path, &cluster, &size, &attr) || !(attr & 0x10)) {
        return false;
    }

    struct scan s;
    scan_begin(&s, cluster);
    char name[FAT_NAME_MAX];
    uint8_t entry_attr;
    uint32_t entry_cluster, entry_size;
    uint32_t seen = 0;
    while (scan_next(&s, name, sizeof(name), &entry_attr, &entry_cluster,
                     &entry_size)) {
        if (name[0] == '.') {
            continue; /* "." and ".." are not files a player cares about */
        }
        if (seen == index) {
            strncpy(name_out, name, name_size - 1);
            name_out[name_size - 1] = '\0';
            *is_dir = (entry_attr & 0x10) != 0;
            *size_out = entry_size;
            return true;
        }
        seen++;
    }
    return false;
}
