/* The pattern: plain data, no behaviour that belongs to the transport.
 *
 * Reproduced from engine/song.py rather than redesigned. The player already
 * knows this model, and it is a considered one - per-track lengths giving
 * polyrhythm, micro-timing in ticks rather than milliseconds, and a set of
 * divisions every one of which divides 24 exactly so none of them drifts
 * against the others.
 */

#ifndef SONG_H
#define SONG_H

#include <stdbool.h>
#include <stdint.h>

#include "audio.h"

#define MAX_STEPS 64
#define STEPS_PER_PAGE 8
#define PAGE_COUNT (MAX_STEPS / STEPS_PER_PAGE)

#define VELOCITY_OFF 0
#define VELOCITY_MIN 1
#define VELOCITY_MAX 127
#define VELOCITY_DEFAULT 100

/* Offsets are stored biased so the on-disk byte is unsigned, which is what
 * msgpack carries and what the Python writes. */
#define OFFSET_BIAS 128

#define LENGTH_MIN 1
#define LENGTH_DEFAULT 8

#define BPM_MIN 20
#define BPM_MAX 300
#define BPM_DEFAULT 120

/* Pulses per quarter note. Every division below divides it exactly. */
#define PPQN 24

enum division {
    DIVISION_QUARTER = 0,   /* 1/4   - 24 ticks */
    DIVISION_EIGHTH,        /* 1/8   - 12 */
    DIVISION_EIGHTH_T,      /* 1/8T  -  8 */
    DIVISION_SIXTEENTH,     /* 1/16  -  6 */
    DIVISION_SIXTEENTH_T,   /* 1/16T -  4 */
    DIVISION_THIRTYSECOND,  /* 1/32  -  3 */
    DIVISION_COUNT,
};

#define DIVISION_DEFAULT DIVISION_SIXTEENTH

/* A sample path, as stored in a song.
 *
 * "/sd/samples/cymbals_crucible-center_1.wav" is the longest that occurs on the
 * card and the Python writes them with the /sd prefix its filesystem mounts
 * under. This firmware mounts the card at the root, so the prefix is stripped
 * on the way in and put back on the way out - the file has to keep meaning the
 * same thing to both. */
#define KIT_PATH_MAX 48

struct song {
    uint8_t steps[TRACK_COUNT][MAX_STEPS];   /* velocity; 0 is off */
    uint8_t offsets[TRACK_COUNT][MAX_STEPS]; /* biased by OFFSET_BIAS */
    uint8_t lengths[TRACK_COUNT];
    uint8_t muted[TRACK_COUNT];
    /* Per-track level, 0.0-2.0 in the Python. Held here as Q12 so 1.0 is 4096
     * and the ceiling is 8192 - integers, because nothing in the audio path
     * should need a float to decide how loud something is. */
    uint16_t volume_q12[TRACK_COUNT];
    uint8_t division;
    uint16_t bpm;

    /* Which sample each track plays. Empty means the track has no opinion and
     * the default kit is used, which is what None means in the Python. */
    char kit[TRACK_COUNT][KIT_PATH_MAX];
};

void song_init(struct song *song);

uint32_t song_ticks_per_step(const struct song *song);
const char *song_division_name(const struct song *song);

/* The furthest a hit may sit from its own grid line: strictly less than half a
 * step. A hit exactly halfway is equidistant between two grid lines and neither
 * step can be said to own it - which loses the hit at best and double-fires it
 * at worst. */
int32_t song_max_offset(const struct song *song);

bool song_is_on(const struct song *song, uint8_t track, uint32_t step);
uint8_t song_velocity(const struct song *song, uint8_t track, uint32_t step);
int32_t song_offset(const struct song *song, uint8_t track, uint32_t step);

void song_set_step(struct song *song, uint8_t track, uint32_t step,
                   uint8_t velocity, int32_t offset);
void song_clear_step(struct song *song, uint8_t track, uint32_t step);
bool song_toggle_step(struct song *song, uint8_t track, uint32_t step);

void song_set_length(struct song *song, uint8_t track, uint32_t steps);
void song_set_bpm(struct song *song, int32_t bpm);
void song_set_division(struct song *song, int32_t division);

/* The longest track, which is what the display pages over. */
uint32_t song_length(const struct song *song);
bool song_is_empty(const struct song *song);

/* Set a track's sample path, stripping a leading "/sd" if the caller kept it. */
void song_set_kit_path(struct song *song, uint8_t track, const char *path);

#endif /* SONG_H */
