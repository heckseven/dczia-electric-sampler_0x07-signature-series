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

    /* How hard recorded hits are pulled onto the grid.
     *
     * Applied here, on the way out, rather than when the hit was captured -
     * which is engine/quantize.py's arrangement and the right one. The offset
     * stored in the song is what the player actually did, so the knob stays
     * reversible: dialling strength back returns the original feel, instead of
     * finding it was thrown away at capture. */
    uint8_t strength;

    /* How many sync pulses go out per quarter note. 2 is the Volca and Pocket
     * Operator convention and the default; 24 matches MIDI clock and DIN sync.
     * Every value divides PPQN exactly, so no rate drifts against the beat. */
    uint8_t sync_ppqn;

    /* --- the external clock ------------------------------------------------
     *
     * Latched by the first incoming pulse and held until the transport stops,
     * which is engine/clock.py's rule. While external, the tick period comes
     * from the gap between pulses instead of from the tempo, and the phase is
     * pulled onto each arriving pulse.
     *
     * If pulses stop the clock does not stall: it free-runs at the last period
     * it measured and re-synchronises when they come back, so a master that
     * pauses or a cable that blinks does not halt the badge mid-pattern. */
    bool external;
    uint64_t ext_per_tick_q32; /* frames per internal tick, while external */

    /* The last few gaps between pulses, in microseconds, averaged to a period.
     *
     * Averaged even though the timestamps are good: a master's own jitter is
     * real, and it is what is left once the measurement stops being the
     * problem. Four is enough for that - engine/clock.py needs up to eight
     * because it is averaging away millisecond quantisation as well. */
    uint32_t gaps[4];
    uint8_t gap_count;
    uint8_t gap_next;
    uint32_t last_pulse_us;

    uint32_t ext_pulses;   /* accepted */
    uint32_t ext_rejected; /* outside the range a tempo can be */

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

/* The sync rates the jack can speak, from engine/clock.py's SYNC_RATES. */
#define SYNC_PPQN_DEFAULT 2

void seq_init(struct seq *seq, struct song *song);
bool seq_set_sync_ppqn(struct seq *seq, uint32_t ppqn);

/* Gaps outside this range are noise or a master that stopped and restarted,
 * not a tempo - engine/clock.py's MIN_PULSE_MS and MAX_PULSE_MS. */
#define EXT_MIN_GAP_US 2000u
#define EXT_MAX_GAP_US 3000000u

/* One pulse arrived on the sync input, timestamped in its own interrupt.
 *
 * Latches the clock to external, measures the period, and pulls the phase onto
 * the pulse. Starts the transport if it was stopped, which is what a master
 * sending clock means. */
void seq_external_pulse(struct seq *seq, uint32_t at_us);

/* The tempo actually being played, which is the song's while internal and the
 * measured one while external. Whole BPM, for the display. */
uint32_t seq_effective_bpm(const struct seq *seq);

/* A tick for anything that should move with the music but must not stop when
 * the music does - the light strip.
 *
 * The transport's own tick while it is running, and a free-running one at the
 * current tempo otherwise. engine/animation.py's whole point is that the
 * animations are a function of the tick rather than of a wall clock; keeping
 * that true while stopped means the strip is still at the tempo when the
 * transport starts, rather than jumping to catch up. */
uint64_t seq_display_tick(const struct seq *seq);

/* How far from its grid line a hit actually plays, once strength is applied.
 * Exposed so it can be tested on its own, and so anything drawing a step can
 * show where it will sound rather than where it was struck. */
int32_t seq_effective_offset(int32_t offset, uint8_t strength);
void seq_start(struct seq *seq);
void seq_stop(struct seq *seq);
void seq_toggle(struct seq *seq);

/* Schedule everything due. Safe to call late: lateness moves when hits are
 * *scheduled*, not when they *sound*. */
void seq_update(struct seq *seq);

uint32_t seq_step_of(const struct seq *seq, uint8_t track);

/* Where the transport is *audibly*, for recording a hit the player just made.
 *
 * Not seq->tick, which is deliberately ahead: the sequencer books hits up to
 * eight blocks early so they can be handed to the mixer on an exact frame, and
 * a pad struck now belongs where the player heard the beat, not where the
 * booking has already reached. So this reads the audio frame counter instead
 * and works backwards.
 *
 * Returns the nearest step on `track` and how far the hit sat from it, in
 * ticks, signed - early is negative. False if the transport is not running,
 * in which case there is no "now" to record against. */
bool seq_now(const struct seq *seq, uint8_t track, uint32_t *step_out,
             int32_t *offset_out);

#endif /* SEQ_H */
