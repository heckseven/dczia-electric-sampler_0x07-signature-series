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
    uint64_t per_tick = frames_per_tick_q32(song);
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
                int32_t offset = song_offset(song, (uint8_t)t, step);
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
