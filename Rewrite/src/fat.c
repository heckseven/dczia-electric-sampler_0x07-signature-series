/* See fat.h. Read-only FAT32, long names included. */

#include <string.h>

#include "pico.h"

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
    /* Both needed for writing: a chain has to go into every copy of the FAT, or
     * the next thing to mount the card picks whichever copy it prefers and sees
     * a different filesystem. */
    uint32_t num_fats;
    uint32_t sectors_per_fat;
    uint32_t total_clusters;
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
    volume.num_fats = num_fats;
    volume.sectors_per_fat = sectors_per_fat;

    uint32_t total_sectors = le32(&bpb[32]);
    if (total_sectors == 0) {
        total_sectors = le16(&bpb[19]);
    }
    uint32_t data_sectors =
        total_sectors > (reserved + num_fats * sectors_per_fat)
            ? total_sectors - (reserved + num_fats * sectors_per_fat)
            : 0;
    volume.total_clusters = data_sectors / sectors_per_cluster;

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

/* --- writing --------------------------------------------------------------- *
 *
 * The ordering is the whole design, and it is what makes this safer than the
 * Python's write-temp-delete-rename:
 *
 *   1. Allocate free clusters and write the data. Nothing existing is touched,
 *      and the old file is still whole and still referenced.
 *   2. Write the chain into every copy of the FAT. Still nothing points at the
 *      new clusters, so they are merely reserved.
 *   3. Rewrite the directory entry - one 32-byte change inside one 512-byte
 *      sector, a single block write, the smallest unit this device can fail to
 *      complete.
 *   4. Free the old chain.
 *
 * Power loss before 3 leaves the old file intact and some clusters leaked.
 * Power loss after it leaves the new file intact and the old clusters leaked.
 * At no point does the file not exist, and no failure crosses two files.
 *
 * Leaked clusters are the accepted cost: they waste space until the card is
 * checked, and that is a failure a player can live with.
 */

/* Writing goes through its own buffer rather than the read cache, which would
 * otherwise be invalidated under the caller mid-operation. */
static uint8_t scratch[SD_BLOCK];

static void put16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void put32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

/* Set one FAT entry, in every copy. Verified, because a wrong byte here is a
 * card that will not mount rather than a file that reads oddly. */
static bool fat_set(uint32_t cluster, uint32_t value) {
    uint32_t offset = cluster * 4;
    uint32_t within = offset % SD_BLOCK;
    uint32_t sector = offset / SD_BLOCK;

    for (uint32_t copy = 0; copy < volume.num_fats; copy++) {
        uint32_t lba = volume.fat_lba + copy * volume.sectors_per_fat + sector;
        if (!sd_read(lba, scratch)) {
            return false;
        }
        /* The top four bits of a FAT32 entry are reserved and must be kept. */
        uint32_t existing = le32(&scratch[within]) & 0xF0000000u;
        put32(&scratch[within], existing | (value & 0x0FFFFFFFu));
        if (!sd_write_verified(lba, scratch)) {
            return false;
        }
    }
    cached_lba = 0xFFFFFFFFu; /* the read cache may now be stale */
    return true;
}

/* The first free cluster at or after `from`, or 0 if the volume is full. */
static uint32_t fat_find_free(uint32_t from) {
    for (uint32_t c = from < 2 ? 2 : from; c < volume.total_clusters + 2; c++) {
        if (fat_next(c) == 0) {
            return c;
        }
    }
    return 0;
}

static bool fat_free_chain(uint32_t cluster) {
    while (cluster >= 2 && cluster < 0x0FFFFFF8u) {
        uint32_t next = fat_next(cluster);
        if (!fat_set(cluster, 0)) {
            return false;
        }
        cluster = next;
    }
    return true;
}

/* Where a directory entry physically lives, so it can be rewritten in place. */
struct entry_site {
    uint32_t lba;
    uint32_t index; /* 32-byte slot within that sector */
    bool found;
};

static bool directory_cluster(const char *path, uint32_t *cluster_out,
                              char *leaf, uint32_t leaf_size);

/* Where an entry lives, and where its long-name run begins.
 *
 * Matching has to be on the long name, not the 8.3 one. "csave.song" is stored
 * as the short name CSAVE~1.SON with a long-name run in front of it, and a
 * lookup that compares against the short form finds nothing - which is how an
 * earlier version of this file created a second entry beside the first instead
 * of replacing it. Two entries of the same name is not a filesystem any checker
 * calls clean, and it was on the player's card.
 */
static bool locate(uint32_t cluster, const char *want, uint32_t *lba_out,
                   uint32_t *index_out) {
    struct scan s;
    scan_begin(&s, cluster);
    char name[FAT_NAME_MAX];
    uint8_t attr;
    uint32_t entry_cluster, entry_size;

    while (scan_next(&s, name, sizeof(name), &attr, &entry_cluster,
                     &entry_size)) {
        if (names_equal(name, want)) {
            /* scan_next has already stepped past the entry, so the one that
             * matched is the slot before wherever it now sits. */
            uint32_t index = s.index == 0 ? (SD_BLOCK / 32) - 1 : s.index - 1;
            uint32_t sector = s.index == 0 ? s.sector - 1 : s.sector;
            *lba_out = cluster_lba(s.cluster) + sector;
            *index_out = index;
            return true;
        }
    }
    return false;
}

/* A run of `count` consecutive free slots inside one sector.
 *
 * Confined to a single sector on purpose: a long name plus its entry is at most
 * six slots, a sector holds sixteen, and keeping the run in one sector means
 * the whole name lands in one block write rather than two that can be
 * interrupted between. */
static bool free_run(uint32_t cluster, uint32_t count, uint32_t *lba_out,
                     uint32_t *index_out) {
    uint32_t walk = cluster;
    while (walk >= 2 && walk < 0x0FFFFFF8u) {
        for (uint32_t sector = 0; sector < volume.sectors_per_cluster;
             sector++) {
            uint32_t lba = cluster_lba(walk) + sector;
            if (!read_cached(lba)) {
                return false;
            }
            uint32_t run = 0;
            for (uint32_t i = 0; i < SD_BLOCK / 32; i++) {
                uint8_t first = cache[i * 32];
                if (first == 0x00 || first == 0xE5) {
                    if (run == 0) {
                        *index_out = i;
                    }
                    run++;
                    if (run == count) {
                        *lba_out = lba;
                        return true;
                    }
                } else {
                    run = 0;
                }
            }
        }
        walk = fat_next(walk);
    }
    return false;
}

/* The checksum that ties a long-name run to its 8.3 entry. Get this wrong and
 * other systems treat the run as orphaned. */
static uint8_t short_checksum(const uint8_t field[11]) {
    uint8_t sum = 0;
    for (uint32_t i = 0; i < 11; i++) {
        sum = (uint8_t)(((sum & 1) << 7) + (sum >> 1) + field[i]);
    }
    return sum;
}

/* Turn "kick song.SONG" into the padded 8.3 field a directory entry carries.
 *
 * Deliberately not long-name writing. A long name is a run of extra entries
 * whose checksum has to match the short one, and getting that wrong produces a
 * directory another system will disagree about. An 8.3 name is eleven bytes
 * with no cross-references, and it is what this phase needs. */
static void to_short_field(const char *leaf, uint8_t field[11]) {
    memset(field, ' ', 11);
    uint32_t n = 0;
    const char *dot = NULL;
    for (const char *p = leaf; *p; p++) {
        if (*p == '.') {
            dot = p;
        }
    }
    for (const char *p = leaf; *p && n < 8; p++) {
        if (dot && p >= dot) {
            break;
        }
        field[n++] = (uint8_t)upper(*p);
    }
    if (dot) {
        uint32_t e = 0;
        for (const char *p = dot + 1; *p && e < 3; p++) {
            field[8 + e++] = (uint8_t)upper(*p);
        }
    }

    /* The ~1 tail, which is what marks a short name as the stand-in for a
     * longer one. Without it "csave.song" and "csave.sonata" would both
     * shorten to CSAVE.SON and refer to each other's clusters. */
    if (n > 6) {
        n = 6;
    }
    field[n] = '~';
    field[n + 1] = '1';
    for (uint32_t i = n + 2; i < 8; i++) {
        field[i] = ' ';
    }
}

/* Mark an entry deleted and release what it held.
 *
 * Matches through `locate`, on the long name, for the same reason writing does:
 * "csave.song" is stored as CSAVE~1.SON, and a delete that compares against the
 * short form silently does nothing. That is how a stale file survived a delete
 * here and sent the next save down the overwrite path instead of the create
 * path - which is a harmless outcome hiding an unhelpful one.
 *
 * Clears the long-name run as well as the entry. A run of fragments with no
 * entry behind them is exactly what a checker complains about.
 */
bool fat_delete(const char *path) {
    if (!volume.mounted) {
        return false;
    }
    char leaf[FAT_NAME_MAX];
    uint32_t dir_cluster;
    if (!directory_cluster(path, &dir_cluster, leaf, sizeof(leaf))) {
        return false;
    }

    uint32_t lba, index;
    if (!locate(dir_cluster, leaf, &lba, &index)) {
        return false;
    }
    if (!sd_read(lba, scratch)) {
        return false;
    }

    uint8_t *entry = &scratch[index * 32];
    uint32_t first = ((uint32_t)le16(&entry[20]) << 16) | le16(&entry[26]);
    entry[0] = 0xE5;
    for (int32_t j = (int32_t)index - 1; j >= 0; j--) {
        uint8_t *prior = &scratch[j * 32];
        if ((prior[11] & 0x0F) != 0x0F || prior[0] == 0xE5) {
            break;
        }
        prior[0] = 0xE5;
    }

    if (!sd_write_verified(lba, scratch)) {
        return false;
    }
    cached_lba = 0xFFFFFFFFu;

    if (first >= 2) {
        (void)fat_free_chain(first);
    }
    return true;
}

bool fat_write(const char *path, const uint8_t *data, uint32_t length) {
    if (!volume.mounted || length == 0) {
        return false;
    }

    char leaf[FAT_NAME_MAX];
    uint32_t dir_cluster;
    if (!directory_cluster(path, &dir_cluster, leaf, sizeof(leaf))) {
        return false;
    }

    /* Step 1: allocate and fill. Nothing existing is touched yet. */
    uint32_t cluster_bytes = volume.sectors_per_cluster * SD_BLOCK;
    uint32_t needed = (length + cluster_bytes - 1) / cluster_bytes;
    uint32_t chain[64];
    if (needed == 0 || needed > count_of(chain)) {
        return false;
    }

    uint32_t search = 2;
    for (uint32_t i = 0; i < needed; i++) {
        uint32_t c = fat_find_free(search);
        if (c == 0) {
            return false; /* full - nothing has been changed */
        }
        chain[i] = c;
        search = c + 1;
    }

    for (uint32_t i = 0; i < needed; i++) {
        for (uint32_t sector = 0; sector < volume.sectors_per_cluster;
             sector++) {
            uint32_t at = i * cluster_bytes + sector * SD_BLOCK;
            if (at >= length) {
                break;
            }
            uint32_t remaining = length - at;
            uint32_t chunk = remaining < SD_BLOCK ? remaining : SD_BLOCK;
            memset(scratch, 0, SD_BLOCK);
            memcpy(scratch, &data[at], chunk);
            if (!sd_write(cluster_lba(chain[i]) + sector, scratch)) {
                return false;
            }
        }
    }

    /* Step 2: publish the chain. Still unreferenced by any directory entry. */
    for (uint32_t i = 0; i < needed; i++) {
        uint32_t next = (i + 1 < needed) ? chain[i + 1] : 0x0FFFFFFFu;
        if (!fat_set(chain[i], next)) {
            return false;
        }
    }

    /* Step 3: the single write that makes it real. */
    uint32_t entry_lba = 0, entry_index = 0;
    bool creating = !locate(dir_cluster, leaf, &entry_lba, &entry_index);

    uint8_t field[11];
    uint32_t lfn_slots = 0;
    if (creating) {
        /* A short name that will not collide, and a long-name run carrying the
         * name the caller actually asked for. */
        to_short_field(leaf, field);
        uint32_t name_length = 0;
        while (leaf[name_length]) {
            name_length++;
        }
        lfn_slots = (name_length + 12) / 13;
        if (lfn_slots > 5) {
            return false; /* longer than this phase writes */
        }
        if (!free_run(dir_cluster, lfn_slots + 1, &entry_lba, &entry_index)) {
            return false; /* directory full - no growing it in this phase */
        }
        entry_index += lfn_slots; /* the 8.3 entry sits after its run */
    }

    if (!sd_read(entry_lba, scratch)) {
        return false;
    }
    uint8_t *entry = &scratch[entry_index * 32];
    uint32_t old_cluster = creating ? 0
                                    : (((uint32_t)le16(&entry[20]) << 16) |
                                       le16(&entry[26]));

    if (creating) {
        memset(entry, 0, 32);
        memcpy(entry, field, 11);
        entry[11] = 0x20; /* archive */

        uint8_t sum = short_checksum(field);
        for (uint32_t part = 0; part < lfn_slots; part++) {
            /* Fragments are stored last-first, so fragment `part` sits
             * `part + 1` slots before the 8.3 entry. */
            uint8_t *fragment = &scratch[(entry_index - 1 - part) * 32];
            memset(fragment, 0, 32);
            fragment[0] = (uint8_t)(part + 1);
            if (part + 1 == lfn_slots) {
                fragment[0] |= 0x40; /* last fragment of the name */
            }
            fragment[11] = 0x0F;
            fragment[13] = sum;

            static const uint8_t offsets[13] = {1,  3,  5,  7,  9,  14, 16,
                                                18, 20, 22, 24, 28, 30};
            for (uint32_t i = 0; i < 13; i++) {
                uint32_t at = part * 13 + i;
                uint16_t unit;
                if (leaf[at] != '\0') {
                    unit = (uint16_t)(uint8_t)leaf[at];
                } else if (at == 0 || leaf[at - 1] != '\0') {
                    unit = 0; /* the terminator, once */
                } else {
                    unit = 0xFFFF; /* padding past the end */
                }
                put16(&fragment[offsets[i]], unit);
            }
        }
    }

    put16(&entry[20], (uint16_t)(chain[0] >> 16));
    put16(&entry[26], (uint16_t)(chain[0] & 0xFFFF));
    put32(&entry[28], length);

    if (!sd_write_verified(entry_lba, scratch)) {
        return false;
    }
    cached_lba = 0xFFFFFFFFu;

    /* Step 4: release what the file used to occupy. A failure here leaks
     * clusters and loses nothing. */
    if (!creating && old_cluster >= 2) {
        (void)fat_free_chain(old_cluster);
    }
    return true;
}

/* Split "/songs/name.song" into the directory's cluster and the leaf name. */
static bool directory_cluster(const char *path, uint32_t *cluster_out,
                              char *leaf, uint32_t leaf_size) {
    const char *last = path;
    for (const char *p = path; *p; p++) {
        if (*p == '/') {
            last = p + 1;
        }
    }
    /* Zero the whole buffer, not just terminate it. The long-name writer reads
     * past the terminator to decide where padding starts, and uninitialised
     * bytes there put garbage into the name. */
    memset(leaf, 0, leaf_size);
    uint32_t n = 0;
    while (last[n] && n < leaf_size - 1) {
        leaf[n] = last[n];
        n++;
    }
    leaf[n] = '\0';
    if (n == 0) {
        return false;
    }

    /* Everything before the leaf is the directory. */
    char parent[FAT_NAME_MAX * 2];
    uint32_t plen = (uint32_t)(last - path);
    if (plen >= sizeof(parent)) {
        return false;
    }
    memcpy(parent, path, plen);
    parent[plen] = '\0';

    uint32_t cluster, size;
    uint8_t attr;
    if (!find(parent, &cluster, &size, &attr) || !(attr & 0x10)) {
        return false;
    }
    *cluster_out = cluster;
    return true;
}
