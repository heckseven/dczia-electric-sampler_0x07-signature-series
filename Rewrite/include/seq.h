/* Transport: what plays, and exactly when.
 *
 * The clock is the audio frame counter rather than a millisecond timer, which
 * is the single decision this phase turns on. See the note at the top of seq.c
 * for why, and for what it costs.
 */

#ifndef SEQ_H
#define SEQ_H

#include <stdbool.h>
#include <stdint.h>

#include "song.h"

struct seq {
    struct song *song;
    bool running;

    /* The frame the transport started on. Every step boundary is measured from
     * here, so nothing accumulates rounding. */
    uint64_t start_frame;
    uint64_t next_tick; /* the next tick to resolve */

    /* When that tick lands, as whole frames plus a 32-bit fraction.
     *
     * Accumulated rather than computed from a multiply: at 120 BPM a tick is
     * 333.33 frames, and rounding it to 333 loses two and a half seconds an
     * hour, while `start + per_tick * n` in 32.32 overflows after about two
     * days of continuous play. */
    uint64_t next_frame;
    uint32_t next_frac;
    uint64_t tick;      /* the last tick resolved, for display */

    /* Where each track's playhead is. Per track, because tracks have their own
     * lengths and therefore their own cycles. */
    uint32_t position[TRACK_COUNT];

    uint32_t hits;

    /* The exact frame the most recent hit was scheduled for, tick boundary plus
     * its sub-block offset. Exposed so the timing can be checked against what
     * actually left the pin rather than against the schedule's own arithmetic,
     * which would only ever agree with itself. */
    uint64_t last_hit_frame;
};

void seq_init(struct seq *seq, struct song *song);
void seq_start(struct seq *seq);
void seq_stop(struct seq *seq);
void seq_toggle(struct seq *seq);

/* Schedule everything due. Safe to call late: lateness moves when hits are
 * *scheduled*, not when they *sound*. */
void seq_update(struct seq *seq);

uint32_t seq_step_of(const struct seq *seq, uint8_t track);

#endif /* SEQ_H */
