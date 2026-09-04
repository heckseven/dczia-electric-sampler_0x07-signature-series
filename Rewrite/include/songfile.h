/* Loading a song off the card, in the format the Python already writes.
 *
 * Reading the existing format rather than inventing one is deliberate: a new
 * format means either migrating the player's songs or stranding them, and a
 * reader for the small corner of msgpack that store.Store uses is a couple of
 * hundred lines.
 */

#ifndef SONGFILE_H
#define SONGFILE_H

#include "song.h"

#define SONG_DIR "/songs"
#define SONG_SUFFIX ".song"

enum songfile_result {
    SONGFILE_OK = 0,
    SONGFILE_NO_FILE,
    SONGFILE_TOO_BIG,
    SONGFILE_SHORT,
    SONGFILE_NOT_A_SONG,
};

enum songfile_result songfile_load(const char *path, struct song *song);
/* Write a song where CircuitPython can read it.
 *
 * The safety of the write itself belongs to fat_write - data, then chain, then
 * one directory entry - so that a power cut leaves either the old song or the
 * new one and never neither. */
enum songfile_result songfile_save(const char *path, const struct song *song);

/* The format itself, with no filesystem attached.
 *
 * Split out so it can be round-tripped on a host. Every bug this format has had
 * - a float encoder that doubled its input, a reader desynchronised by an
 * unexpected nil, keys arriving in hash order, and two settings silently
 * dropped on write - was in here and none of them needed a card to reproduce.
 * They were found on hardware anyway, at the cost of a power pull each. */
enum songfile_result songfile_decode(const uint8_t *data, uint32_t length,
                                     struct song *song);
enum songfile_result songfile_encode(uint8_t *out, uint32_t capacity,
                                     const struct song *song,
                                     uint32_t *length_out);

const char *songfile_result_name(enum songfile_result result);

#endif /* SONGFILE_H */
