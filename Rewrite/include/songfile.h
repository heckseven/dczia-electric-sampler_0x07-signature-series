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
const char *songfile_result_name(enum songfile_result result);

#endif /* SONGFILE_H */
