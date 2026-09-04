/* The transport, counted in frames.
 *
 * The whole point of this file is in one line of arithmetic: a step boundary is
 * a frame number, not a moment. Everything else follows from that.
 *
 * The current firmware advances its clock from the main loop against
 * `ticks_ms()`, so a step fires when the loop next notices it should have -
 * 8.5 ms later typically, 46 ms in the tail, which Phase 0 measured and traced
 * to the garbage collector. At the default 1/16 division a step is 125 ms, so a
 * hit can land more than a third of a step late.
 *
 * Frames do not have that problem. The audio core emits exactly 16,000 a second
 * because a crystal says so, and a boundary computed in frames names a specific
 * sample. The sequencer cannot drift against the audio because it is counting
 * the audio.
 *
 * The cost is that boundaries land inside blocks rather than on them, which is
 * what `audio_trigger_at`'s frame offset exists to absorb.
 */

#include <string.h>

#include "audio.h"
#include "sync.h"
#include "seq.h"

/* How far ahead hits are booked.
 *
 * One block was the obvious choice and the wrong one. Booking a hit for the
 * block about to be mixed leaves no slack at all: any pause in the caller - USB
 * service, a display flush, anything - pushes a tick past its own block, and
 * the mixer then starts that voice immediately rather than when it was asked
 * for. Measured, that showed up as occasional hits arriving 3.8 ms early
 * against their schedule, which is the pipeline delay they had missed.
 *
 * Booking early costs nothing, and that is the whole point of handing the mixer
 * an absolute frame: a hit booked eight blocks out still sounds on exactly the
 * frame it names. So the lookahead only has to exceed the caller's worst pause,
 * and 16 ms is far past anything this loop does while staying well inside a
 * step even at 300 BPM and 1/32.
 */
#define SEQ_LOOKAHEAD_FRAMES (BLOCK_FRAMES * 8)

/* Frames per tick, in 32.32 fixed point.
 *
 * Kept fractional rather than rounded because rounding here is exactly the
 * drift this design exists to avoid: at 120 BPM and 24 PPQN a tick is 333.33
 * frames, and rounding to 333 loses a frame every three ticks - two and a half
 * seconds of error an hour, which is audible against anything else keeping
 * time.
 */
static uint64_t frames_per_tick_q32(const struct song *song) {
    /* frames per tick = rate * 60 / (bpm * PPQN) */
    uint64_t numerator = (uint64_t)SAMPLE_RATE * 60u << 32;
    return numerator / ((uint64_t)song->bpm * PPQN);
}

void seq_init(struct seq *seq, struct song *song) {
    memset(seq, 0, sizeof(*seq));
    seq->song = song;
    seq->running = false;
    seq->strength = STRENGTH_DEFAULT;
    seq->sync_ppqn = SYNC_PPQN_DEFAULT;
}

uint64_t seq_display_tick(const struct seq *seq) {
    if (seq->running) {
        return seq->tick;
    }
    /* From the frame counter, at whatever tempo is current. The same
     * arithmetic as seq_now, and for the same reason: multiplying by
     * bpm * PPQN and dividing by rate * 60 cannot overflow, where dividing by
     * the 32.32 period would after 27 hours. */
    uint64_t bpm = seq_effective_bpm(seq);
    if (bpm == 0) {
        bpm = BPM_DEFAULT;
    }
    return (audio_frames() * bpm * PPQN) / ((uint64_t)SAMPLE_RATE * 60u);
}

uint32_t seq_effective_bpm(const struct seq *seq) {
    if (!seq->external || seq->ext_per_tick_q32 == 0) {
        return seq->song->bpm;
    }
    /* frames per tick -> BPM, inverting frames_per_tick_q32. Rounded, so a
     * measured 119.6 shows as 120 rather than 119. */
    uint64_t numerator = ((uint64_t)SAMPLE_RATE * 60u << 32) * 2u;
    uint64_t bpm = numerator / (seq->ext_per_tick_q32 * PPQN);
    return (uint32_t)((bpm + 1) / 2);
}

void seq_external_pulse(struct seq *seq, uint32_t at_us) {
    uint32_t ppqn = seq->sync_ppqn ? seq->sync_ppqn : 1;
    uint32_t per_pulse = PPQN / ppqn; /* internal ticks between pulses */

    uint32_t gap = at_us - seq->last_pulse_us;
    bool had_previous = (seq->last_pulse_us != 0);
    seq->last_pulse_us = at_us;

    if (had_previous && (gap < EXT_MIN_GAP_US || gap > EXT_MAX_GAP_US)) {
        /* Not a tempo at all. Keep the period already measured and start the
         * history again, rather than believing a gap that cannot be music. */
        seq->gap_count = 0;
        seq->gap_next = 0;
        seq->ext_rejected++;
        return;
    }

    /* A gap several times the established period is a master that paused and
     * came back, not a master that slowed down.
     *
     * engine/clock.py has no such case: the gap goes into the average and the
     * result is clamped to MIN_BPM, so a two second pause drops the badge to
     * 20 BPM and it climbs back over the next few pulses. That contradicts its
     * own docstring - "it keeps running at the last tempo it measured, and
     * re-synchronises when pulses return" - and this is what that sentence
     * describes. The history is discarded, the tempo is kept, and the phase is
     * still pulled onto the pulse below, so the badge lines up with the master
     * again immediately instead of audibly winding back up to speed. */
    /* Keyed on the measured period, not on the history: a glitch just before
     * the pause clears the history but not the tempo, and that combination -
     * bad cable, then a pause - is exactly when this matters most. */
    if (had_previous && seq->ext_per_tick_q32 != 0) {
        uint64_t established =
            (seq->ext_per_tick_q32 >> 32) * per_pulse * 125u / 2u; /* us */
        if (established > 0 && gap > established * 4u) {
            seq->gap_count = 0;
            seq->gap_next = 0;
            had_previous = false;
        }
    }

    if (had_previous) {
        seq->gaps[seq->gap_next] = gap;
        seq->gap_next = (uint8_t)((seq->gap_next + 1u) %
                                  (sizeof(seq->gaps) / sizeof(seq->gaps[0])));
        if (seq->gap_count < sizeof(seq->gaps) / sizeof(seq->gaps[0])) {
            seq->gap_count++;
        }

        uint64_t total = 0;
        for (uint32_t i = 0; i < seq->gap_count; i++) {
            total += seq->gaps[i];
        }
        uint32_t average_us = (uint32_t)(total / seq->gap_count);

        /* Microseconds to frames per internal tick, in 32.32. At 16 kHz a
         * frame is 62.5 us, so frames = us * 2 / 125, and the whole division
         * is done once here rather than per tick. */
        uint64_t pulse_frames_q32 =
            (((uint64_t)average_us * 2u) << 32) / 125u;
        uint64_t measured = pulse_frames_q32 / per_pulse;

        /* Held inside the tempo range the song model allows. A period outside
         * it is not a tempo this instrument can play, and letting it through
         * would put the sequencer somewhere its own arithmetic never goes. */
        uint64_t slowest = ((uint64_t)SAMPLE_RATE * 60u << 32) /
                           ((uint64_t)BPM_MIN * PPQN);
        uint64_t fastest = ((uint64_t)SAMPLE_RATE * 60u << 32) /
                           ((uint64_t)BPM_MAX * PPQN);
        if (measured > slowest) {
            measured = slowest;
        }
        if (measured < fastest) {
            measured = fastest;
        }
        seq->ext_per_tick_q32 = measured;
    }

    seq->ext_pulses++;

    /* A master sending clock to a stopped transport means start. */
    if (!seq->running) {
        seq_start(seq);
        seq->external = true;
        return;
    }
    seq->external = true;
    if (seq->ext_per_tick_q32 == 0) {
        return; /* first pulse: latched, but no period measured yet */
    }

    /* --- pull the phase onto the pulse ---------------------------------- */

    uint64_t pulse_frame;
    if (!audio_frame_at_time_us(at_us, &pulse_frame)) {
        return;
    }

    /* Which tick the schedule would have been on when that frame played.
     * next_frame is where next_tick is due, so stepping back from there says
     * where the pulse fell relative to the grid. */
    int64_t from_next = (int64_t)pulse_frame - (int64_t)seq->next_frame;
    int64_t per_tick_frames = (int64_t)(seq->ext_per_tick_q32 >> 32);
    if (per_tick_frames <= 0) {
        return;
    }
    int64_t ticks_from_next = from_next / per_tick_frames;
    int64_t tick_at_pulse = (int64_t)seq->next_tick + ticks_from_next;

    /* Snap to the nearest pulse boundary. The clock free-runs at the measured
     * period between pulses, so this is a correction of a tick or two rather
     * than an audible jump. */
    int64_t remainder = tick_at_pulse % (int64_t)per_pulse;
    if (remainder < 0) {
        remainder += per_pulse;
    }
    int64_t snapped = tick_at_pulse - remainder;
    if (remainder * 2 >= (int64_t)per_pulse) {
        snapped += per_pulse;
    }

    /* Re-anchor so that `snapped` sits exactly on the pulse, and next_tick
     * follows from it. Only the schedule ahead moves: ticks already resolved
     * have had their hits handed to the mixer on frames that cannot be taken
     * back, which is why this adjusts next_frame rather than start_frame. */
    int64_t ahead = (int64_t)seq->next_tick - snapped;
    int64_t anchored = (int64_t)pulse_frame + ahead * per_tick_frames;

    /* A correction larger than a whole pulse is not a correction. It means the
     * measurement or the master jumped, and following it would fire a burst of
     * catch-up ticks or stall for one. Take the tempo and leave the phase. */
    int64_t move = anchored - (int64_t)seq->next_frame;
    int64_t limit = per_tick_frames * (int64_t)per_pulse;
    if (move > limit || move < -limit) {
        return;
    }
    if (anchored < 0) {
        return;
    }
    seq->next_frame = (uint64_t)anchored;
    seq->next_frac = 0;
}

bool seq_set_sync_ppqn(struct seq *seq, uint32_t ppqn) {
    /* Only rates that divide PPQN, so a pulse always lands on a tick. 24/5 is
     * 4.8 ticks and every fifth pulse would sit somewhere the sequencer never
     * visits. */
    if (ppqn == 1 || ppqn == 2 || ppqn == 4 || ppqn == 24) {
        seq->sync_ppqn = (uint8_t)ppqn;
        return true;
    }
    return false;
}

int32_t seq_effective_offset(int32_t offset, uint8_t strength) {
    if (offset == 0 || strength >= STRENGTH_MAX) {
        return 0;
    }
    /* offset * (1 - strength), in twentieths, rounded to nearest and
     * symmetrically about zero. A tick is the finest thing that can be played,
     * so a residue under half a tick really is on the grid.
     *
     * Worth knowing: at fine divisions an offset spans only a couple of ticks,
     * so the knob has correspondingly few distinguishable settings there and
     * more at coarse ones. That is the resolution the model has rather than a
     * fault in the rounding. */
    int32_t remaining = offset * (int32_t)(STRENGTH_MAX - strength);
    if (remaining > 0) {
        return (remaining + STRENGTH_MAX / 2) / STRENGTH_MAX;
    }
    return -((-remaining + STRENGTH_MAX / 2) / STRENGTH_MAX);
}

void seq_start(struct seq *seq) {
    if (seq->running) {
        return;
    }
    seq->running = true;
    /* Start one lookahead in the future, not now.
     *
     * Tick 0 is due the instant the transport starts, so booking it for the
     * current frame books it for a block already being mixed - and the mixer
     * then starts that voice at the next opportunity rather than a full
     * pipeline later, like every other hit. Measured, the first hit of a
     * pattern landed about 4 ms adrift of the rest.
     *
     * Sixteen milliseconds of delay before the first note is imperceptible on
     * a Play press, and it makes every hit in the pattern - including the
     * first - sound exactly one pipeline after the frame it names. */
    seq->start_frame = audio_frames() + SEQ_LOOKAHEAD_FRAMES;
    seq->next_frame = seq->start_frame;
    seq->next_frac = 0;
    seq->next_tick = 0;
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        seq->position[t] = 0;
    }
    seq->tick = 0;
}

void seq_stop(struct seq *seq) {
    /* Back to the song's own tempo. engine/clock.py latches to external on the
     * first pulse and holds it "until the transport stops" - so this is where
     * it lets go, and the badge plays at its own tempo again rather than at
     * whatever a master that has since been unplugged was doing. */
    seq->external = false;
    seq->last_pulse_us = 0;
    seq->gap_count = 0;
    seq->gap_next = 0;
    seq->running = false;
    audio_stop_all();
}

void seq_toggle(struct seq *seq) {
    if (seq->running) {
        seq_stop(seq);
    } else {
        seq_start(seq);
    }
}

/* Fire everything whose step boundary has arrived, with the sub-block offset
 * that puts each hit on the right sample.
 *
 * Called from the main loop, but its punctuality does not matter: the loop
 * decides when hits are *scheduled*, and the frame offset decides when they
 * *sound*. Being called late means several ticks resolve in one pass, each
 * still landing on its own frame - which is why a busy UI cannot smear the
 * timing the way it does in the Python.
 */
void seq_update(struct seq *seq) {
    if (!seq->running || seq->song == NULL) {
        return;
    }

    const struct song *song = seq->song;
    uint64_t now = audio_frames();
    /* While externally synced the period is measured, not derived from the
     * song's tempo - that is the whole of what being synced means. */
    uint64_t per_tick = seq->external ? seq->ext_per_tick_q32
                                      : frames_per_tick_q32(song);
    uint32_t whole_per_tick = (uint32_t)(per_tick >> 32);
    uint32_t frac_per_tick = (uint32_t)per_tick;
    uint32_t ticks_per_step = song_ticks_per_step(song);

    /* Advance one tick at a time so no step is skipped when the caller is late.
     * A bounded number of iterations: even a 46 ms stall at 1/32 and 300 BPM is
     * a handful of ticks. */
    for (;;) {
        uint64_t tick_frame = seq->next_frame;
        if (tick_frame > now + SEQ_LOOKAHEAD_FRAMES) {
            break;
        }

        uint64_t tick = seq->next_tick;
        seq->tick = tick;
        seq->next_tick++;

        /* Sync out, scheduled for when this tick is *heard* rather than sent
         * now. `tick_frame` is up to a lookahead in the future - that is what
         * lets the mixer place a hit on an exact frame - and a pulse sent at
         * this moment would arrive sixteen milliseconds before the beat it is
         * supposed to mark. */
        uint32_t per_pulse = PPQN / (seq->sync_ppqn ? seq->sync_ppqn : 1);
        if (tick % per_pulse == 0) {
            sync_pulse_at_frame(tick_frame);
        }

        /* Accumulate the next boundary rather than multiplying out to it.
         *
         * `start + per_tick * n` is the obvious spelling and it overflows: at
         * 32.32, per_tick is about 1.4e12 and n reaches 1e7 after roughly two
         * days of continuous play, which is inside the range somebody leaves a
         * sampler running. Carrying a whole part and a fraction is exact, costs
         * an add, and cannot overflow in any lifetime. */
        seq->next_frame += whole_per_tick;
        uint32_t before = seq->next_frac;
        seq->next_frac += frac_per_tick;
        if (seq->next_frac < before) {
            seq->next_frame++;
        }

        for (uint32_t t = 0; t < TRACK_COUNT; t++) {
            uint32_t length = song->lengths[t];
            if (length == 0 || song->muted[t]) {
                continue;
            }

            /* Which step, if any, fires on this tick.
             *
             * A step s sits on grid tick s * ticks_per_step and fires at
             * s * ticks_per_step + offset(s). Solving that backwards is fiddly;
             * checking forwards is not. Because an offset is strictly less than
             * half a step - see song_max_offset, and the reason it is strict -
             * only two steps can possibly reach any given tick: the one whose
             * grid line is at or below it, and the next one up. So test those
             * two and nothing else.
             *
             * The cycle is per track, which is what makes different lengths
             * polyrhythmic: a 3-step track against a 4-step track realigns
             * every 12 without either being aware of the other. */
            uint32_t cycle = length * ticks_per_step;
            uint32_t position = (uint32_t)(tick % cycle);
            uint32_t base = position / ticks_per_step;

            /* Two candidates, unless the track is one step long - then both
             * wrap to the same step and it would fire twice. That is not
             * hypothetical: the timing rig used a one-step track, and the
             * duplicate showed up as occasional hits measured against a tone
             * that was already sounding. */
            uint32_t candidates = (length > 1) ? 2 : 1;
            for (uint32_t which = 0; which < candidates; which++) {
                uint32_t step = (base + which) % length;
                if (!song_is_on(song, (uint8_t)t, step)) {
                    continue;
                }
                /* A track carrying its own strength ignores the global knob,
                 * which is how one track swings while the rest stay straight. */
                int8_t override = song->track_strength[t];
                uint8_t strength =
                    (override < 0) ? seq->strength : (uint8_t)override;
                int32_t offset = seq_effective_offset(
                    song_offset(song, (uint8_t)t, step), strength);
                int32_t fires_at =
                    (int32_t)(step * ticks_per_step) + offset;
                /* Wrap into the cycle: a step 0 nudged early fires at the very
                 * end of the previous pass round. */
                fires_at = ((fires_at % (int32_t)cycle) + (int32_t)cycle) %
                           (int32_t)cycle;
                if ((uint32_t)fires_at != position) {
                    continue;
                }

                /* Velocity is 1-127 in the model and Q15 in the mixer, with
                 * the track's own level riding on top. */
                uint32_t velocity = song_velocity(song, (uint8_t)t, step);
                int32_t gain = (int32_t)((velocity * 0x7FFF) / VELOCITY_MAX);
                gain = (gain * (int32_t)song->volume_q12[t]) >> 12;
                if (gain > 0x7FFF) {
                    gain = 0x7FFF;
                }

                audio_trigger_at_frame((uint8_t)t, tick_frame, (int16_t)gain);
                seq->position[t] = step;
                seq->last_hit_frame = tick_frame;
                seq->hits++;
            }
        }
    }
}

uint32_t seq_step_of(const struct seq *seq, uint8_t track) {
    return track < TRACK_COUNT ? seq->position[track] : 0;
}

bool seq_now(const struct seq *seq, uint8_t track, uint32_t *step_out,
             int32_t *offset_out) {
    if (!seq->running) {
        return false;
    }
    uint64_t now = audio_frames();
    if (now <= seq->start_frame) {
        *step_out = 0;
        *offset_out = 0;
        return true;
    }

    /* Frames since the downbeat, converted to ticks.
     *
     * Not via frames_per_tick_q32: dividing by a 32.32 figure means shifting
     * the frame count up by 32 first, which overflows 64 bits after 2^32
     * frames - 27 hours - and silently records into the wrong bar rather than
     * failing. Multiplying by bpm * PPQN and dividing by rate * 60 is the same
     * arithmetic the other way round, exact, and does not overflow for
     * centuries.
     *
     * Rounded, not truncated. An offset is a whole number of ticks, so the
     * nearest tick is the honest answer, and truncating puts a hit that landed
     * a hair inside the boundary one tick early every time. */
    uint64_t elapsed = now - seq->start_frame;
    uint64_t denominator = (uint64_t)SAMPLE_RATE * 60u;
    uint64_t ticks = (elapsed * seq->song->bpm * PPQN + denominator / 2) /
                     denominator;

    uint32_t per_step = song_ticks_per_step(seq->song);
    uint32_t length = seq->song->lengths[track];
    if (length < 1) {
        length = 1;
    }

    /* Round to the nearest step rather than the one just passed: a hit landing
     * a hair early is the player being early for the *next* beat, not very late
     * for the last one, and truncating would file it under the wrong step and
     * give it an offset nearly a whole step long. */
    uint64_t step = (ticks + per_step / 2) / per_step;
    int32_t offset = (int32_t)(int64_t)(ticks - step * per_step);

    *step_out = (uint32_t)(step % length);
    *offset_out = offset;
    return true;
}
