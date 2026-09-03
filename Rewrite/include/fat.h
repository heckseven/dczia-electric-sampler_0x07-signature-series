/* Read-only FAT32, enough to find a file by path and read it.
 *
 * Not FatFS. Reentrancy in FatFS - a permanently latched `fp->err` once two
 * paths touched the same file object - is what broke streaming in the
 * CircuitPython firmware, and it is documented at length in
 * docs/streaming-bug-rootcause.md. Phase 1 reads a handful of files at load
 * time and never writes, which is a small enough job to own outright.
 *
 * Long filenames are supported because the card requires it: the kit is
 * `hh_hats-closed_1.wav` and `snare_kraken-head_1.wav`, neither of which fits
 * 8.3. Matching the mangled short names instead would work until somebody
 * added a second file with the same six-character prefix.
 */

#ifndef FAT_H
#define FAT_H

#include <stdbool.h>
#include <stdint.h>

/* One directory entry's worth of name. FAT32 allows 255; this is what a sample
 * name plausibly needs, and a longer one is skipped rather than truncated into
 * a false match. */
#define FAT_NAME_MAX 64

struct fat_file {
    uint32_t first_cluster;
    uint32_t size;     /* bytes, from the directory entry */
    uint32_t position; /* bytes consumed so far */
    uint32_t cluster;  /* cluster holding `position` */
    uint32_t cluster_offset;
};

/* Find the FAT32 volume and read its layout. Looks for an MBR partition first
 * and falls back to a filesystem at block 0. */
bool fat_mount(void);

/* Open by absolute path, e.g. "/samples/kick_crater.wav". Case-insensitive,
 * which is what FAT itself is. */
bool fat_open(const char *path, struct fat_file *file);

/* Read up to `length` bytes. Returns how many were actually read - short at the
 * end of the file, zero at its end. */
uint32_t fat_read(struct fat_file *file, void *buffer, uint32_t length);

/* Iterate a directory. `index` counts real entries, skipping the long-name
 * fragments and the volume label. Returns false once past the end. */
bool fat_list(const char *path, uint32_t index, char *name_out,
              uint32_t name_size, bool *is_dir, uint32_t *size_out);

/* Write a file whole, into a directory that already exists.
 *
 * The ordering is the point - data, then the FAT chain, then one directory
 * entry, then release the old chain - so that a power cut leaves either the old
 * file or the new one and never neither. See the note above fat_write in fat.c.
 *
 * A new file gets an 8.3 name. Long-name writing is a run of extra entries with
 * a checksum tying them to the short one, and getting that wrong produces a
 * directory other systems disagree about. */
bool fat_write(const char *path, const uint8_t *data, uint32_t length);

/* Remove a file and release its clusters. Matches on the 8.3 name, so it can
 * clear entries this firmware wrote before it understood long names. */
bool fat_delete(const char *path);

/* Free clusters, by walking a whole FAT copy. Thousands of sectors, so this is
 * for tests: it exists to answer "did repeated saving leak space", which the
 * write path's leak-rather-than-corrupt policy makes a real question. */
uint32_t fat_count_free(void);
uint32_t fat_cluster_bytes(void);

#endif /* FAT_H */
