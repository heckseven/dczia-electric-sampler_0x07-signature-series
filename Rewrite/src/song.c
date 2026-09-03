/* See song.h. Plain data and the arithmetic that belongs to it. */

#include <string.h>

#include "song.h"

/* Ticks per step at 24 PPQN. Every entry divides 24 exactly, so no division
 * drifts against another - the property that lets a 1/8T track and a 1/16 track
 * stay locked to the same bar forever. */
static const uint8_t TICKS[DIVISION_COUNT] = {24, 12, 8, 6, 4, 3};
static const char *const NAMES[DIVISION_COUNT] = {"1/4",  "1/8",   "1/8T",
                                                  "1/16", "1/16T", "1/32"};

static int32_t clamp(int32_t value, int32_t low, int32_t high) {
    return value < low ? low : (value > high ? high : value);
}

void song_init(struct song *song) {
    memset(song, 0, sizeof(*song));
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song->lengths[t] = LENGTH_DEFAULT;
        song->volume_q12[t] = 4096; /* 1.0 */
        for (uint32_t s = 0; s < MAX_STEPS; s++) {
            song->offsets[t][s] = OFFSET_BIAS;
        }
    }
    song->division = DIVISION_DEFAULT;
    song->bpm = BPM_DEFAULT;
}

uint32_t song_ticks_per_step(const struct song *song) {
    return TICKS[song->division < DIVISION_COUNT ? song->division
                                                 : DIVISION_DEFAULT];
}

const char *song_division_name(const struct song *song) {
    return NAMES[song->division < DIVISION_COUNT ? song->division
                                                 : DIVISION_DEFAULT];
}

int32_t song_max_offset(const struct song *song) {
    return (int32_t)((song_ticks_per_step(song) - 1) / 2);
}

bool song_is_on(const struct song *song, uint8_t track, uint32_t step) {
    if (track >= TRACK_COUNT || step >= MAX_STEPS) {
        return false;
    }
    return song->steps[track][step] != VELOCITY_OFF;
}

uint8_t song_velocity(const struct song *song, uint8_t track, uint32_t step) {
    if (track >= TRACK_COUNT || step >= MAX_STEPS) {
        return VELOCITY_OFF;
    }
    return song->steps[track][step];
}

int32_t song_offset(const struct song *song, uint8_t track, uint32_t step) {
    if (track >= TRACK_COUNT || step >= MAX_STEPS) {
        return 0;
    }
    return (int32_t)song->offsets[track][step] - OFFSET_BIAS;
}

void song_set_step(struct song *song, uint8_t track, uint32_t step,
                   uint8_t velocity, int32_t offset) {
    if (track >= TRACK_COUNT || step >= MAX_STEPS) {
        return;
    }
    int32_t limit = song_max_offset(song);
    song->steps[track][step] =
        (uint8_t)clamp(velocity, VELOCITY_OFF, VELOCITY_MAX);
    song->offsets[track][step] =
        (uint8_t)(clamp(offset, -limit, limit) + OFFSET_BIAS);
}

void song_clear_step(struct song *song, uint8_t track, uint32_t step) {
    if (track >= TRACK_COUNT || step >= MAX_STEPS) {
        return;
    }
    song->steps[track][step] = VELOCITY_OFF;
    song->offsets[track][step] = OFFSET_BIAS;
}

bool song_toggle_step(struct song *song, uint8_t track, uint32_t step) {
    /* Tap behaviour: a lit pad clears, an unlit pad records a hit. */
    if (song_is_on(song, track, step)) {
        song_clear_step(song, track, step);
        return false;
    }
    song_set_step(song, track, step, VELOCITY_DEFAULT, 0);
    return true;
}

void song_set_length(struct song *song, uint8_t track, uint32_t steps) {
    if (track >= TRACK_COUNT) {
        return;
    }
    song->lengths[track] = (uint8_t)clamp((int32_t)steps, LENGTH_MIN, MAX_STEPS);
}

void song_set_bpm(struct song *song, int32_t bpm) {
    song->bpm = (uint16_t)clamp(bpm, BPM_MIN, BPM_MAX);
}

void song_set_division(struct song *song, int32_t division) {
    song->division = (uint8_t)clamp(division, 0, DIVISION_COUNT - 1);
    /* A shorter step means a smaller legal offset, and a hit left beyond the
     * new limit would sit closer to a neighbouring grid line than its own. */
    int32_t limit = song_max_offset(song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        for (uint32_t s = 0; s < MAX_STEPS; s++) {
            int32_t offset = (int32_t)song->offsets[t][s] - OFFSET_BIAS;
            song->offsets[t][s] =
                (uint8_t)(clamp(offset, -limit, limit) + OFFSET_BIAS);
        }
    }
}

uint32_t song_length(const struct song *song) {
    uint32_t longest = LENGTH_MIN;
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        if (song->lengths[t] > longest) {
            longest = song->lengths[t];
        }
    }
    return longest;
}

bool song_is_empty(const struct song *song) {
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        for (uint32_t s = 0; s < song->lengths[t]; s++) {
            if (song->steps[t][s] != VELOCITY_OFF) {
                return false;
            }
        }
    }
    return true;
}

void song_set_kit_path(struct song *song, uint8_t track, const char *path) {
    if (track >= TRACK_COUNT) {
        return;
    }
    if (path == NULL || path[0] == '\0') {
        song->kit[track][0] = '\0';
        return;
    }
    /* The Python's filesystem mounts the card at /sd and writes paths to match.
     * This one mounts it at the root. Same file, two names, and a song has to
     * mean the same thing opened in either. */
    if (strncmp(path, "/sd/", 4) == 0) {
        path += 3;
    }
    uint32_t n = 0;
    while (path[n] && n < KIT_PATH_MAX - 1) {
        song->kit[track][n] = path[n];
        n++;
    }
    song->kit[track][n] = '\0';
}
