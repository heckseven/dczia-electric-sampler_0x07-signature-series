/* The audio core: everything that must never be late.
 *
 * Runs on core 1 and owns it. Core 0 talks to it only through the small set of
 * functions below, all of which are safe to call while it is running - they
 * write one word each, and the mixer reads a consistent snapshot per block.
 *
 * The shape is decided by Phase 0 rather than by taste:
 *
 *   32-frame blocks, because trigger-to-output measured as
 *   block_period x (1..2) + 568 us, putting a 32-frame block at 4.56 ms worst
 *   case against a 10 ms budget.
 *
 *   The stream never stops. The MAX98357A takes 7 ms typical to leave standby,
 *   which is 73% of the budget, so idle means streaming silence.
 *
 *   A polling loop rather than an interrupt. Core 1 has nothing else to do, and
 *   a loop that spins on the DMA is both simpler and more deterministic than a
 *   handler that has to be reasoned about against every other interrupt.
 *
 *   Everything here is __not_in_flash_func and calls nothing that is not. That
 *   is what let Phase 0 erase a flash sector for 35 ms with zero underruns and
 *   no measurable jitter, and it is the property that makes saving during
 *   playback possible at all.
 */

#ifndef AUDIO_H
#define AUDIO_H

#include <stdbool.h>
#include <stdint.h>

#include "board.h"

/* Eight pads, each able to overlap its own retrigger once. Phase 0 measured 18
 * interpolated voices at 7.2% of a core and put the ceiling near 250, so this
 * is nowhere near a limit - it is the number the instrument needs. */
#define TRACK_COUNT 8
#define VOICES_PER_TRACK 2
#define VOICE_COUNT (TRACK_COUNT * VOICES_PER_TRACK)

/* One static arena for every sample. No allocator runs after init: the failures
 * in streaming-bug-rootcause.md were allocation and collection, never
 * throughput. 192 KB of the measured 246.8 KB free, which is 6.1 seconds at
 * 16 kHz mono - against the 1.02 s the CircuitPython build could hold. */
#define ARENA_BYTES (192 * 1024)

/* 16.16 fixed point. UNITY is playback at the recorded rate; half is an octave
 * down, double an octave up. */
#define PITCH_UNITY (1u << 16)

void audio_init(void);

/* Hand core 1 the audio loop. After this the stream is live and silent, and it
 * stays live - see the note above about the amp's 7 ms wake-up. */
void audio_start(void);

/* --- the arena ------------------------------------------------------------ */

/* Claim space for a sample. Returns NULL when the arena is full, which is a
 * normal answer and not an error: a kit that does not fit is a kit the player
 * has to make smaller. */
int16_t *audio_arena_alloc(uint32_t frames);

/* Drop every claim. The only way to free: samples are released as a kit, never
 * one at a time, so a bump allocator is the honest data structure. Every voice
 * is silenced first - a voice left pointing into a reset arena would play
 * whatever is loaded next. */
void audio_arena_reset(void);

uint32_t audio_arena_used(void);
uint32_t audio_arena_free(void);

/* --- tracks --------------------------------------------------------------- */

/* Point a track at a sample. Silences any voice already playing it, because a
 * track whose sample changed underneath a sounding voice would play half of
 * one sound and half of another. */
void audio_set_sample(uint8_t track, const int16_t *data, uint32_t frames);

/* 16.16 rate. PITCH_UNITY is the recorded rate. */
void audio_set_pitch(uint8_t track, uint32_t step);

/* Q15. Applied per track, on top of the master. */
void audio_set_gain(uint8_t track, int16_t gain);

/* Start the track's sample from the beginning, on whichever of its two voices
 * is free - or the older one if both are busy, so a fast retrigger cuts its own
 * tail rather than being dropped. */
void audio_trigger(uint8_t track);

/* Trigger on an exact frame, at Q15 `velocity`. Zero means as soon as possible.
 *
 * Absolute rather than an offset, and the difference is not cosmetic. An offset
 * has to be measured from something, and the only "now" available to core 0 is
 * a frame count that advances a block at a time - so the block the caller means
 * and the block the mixer fills next need not be the same one. Measured, that
 * was 5.7 ms of spread on sequenced hits. Handing over the target frame lets
 * the mixer decide, which is the only place that knows. */
void audio_trigger_at_frame(uint8_t track, uint64_t at_frame,
                            int16_t velocity);

/* Frames emitted since the stream started - the sequencer's clock.
 *
 * Counting the same thing the audio counts is what stops the two drifting
 * apart, and this is a crystal rather than a main loop. */
uint64_t audio_frames(void);

/* When a frame reaches the pin, on the hardware microsecond timer.
 *
 * Not when it was mixed - audio_frames counts frames the mixer has produced,
 * and the DAC is a block or two behind that. Anything that has to line up with
 * what is *heard* needs this instead: the sync jack, and anything else later
 * that leaves the badge alongside the audio.
 *
 * False before the first block has played, when there is no mapping yet. The
 * answer may be in the past for a frame already gone. */
bool audio_frame_time_us(uint64_t frame, uint32_t *when_us);

/* And back the other way: which frame was leaving the pin at a given moment.
 * For timestamps captured in an interrupt - a sync edge - which need placing on
 * the same timeline the sequencer schedules against. */
bool audio_frame_at_time_us(uint32_t when_us, uint64_t *frame_out);

void audio_stop(uint8_t track);
void audio_stop_all(void);

/* Q15 master, applied after the per-track gain. */
void audio_set_master(int16_t gain);

/* --- telemetry ------------------------------------------------------------ */

/* A block whose mix did not finish before the hardware needed it. Should stay
 * at zero; anything else is the number Phase 1 is judged on. */
uint32_t audio_underruns(void);

/* Blocks mixed since start, and the worst mix time seen, in CPU cycles at
 * 125 MHz. A 32-frame block has 250,000 cycles to play with. */
uint32_t audio_blocks(void);
uint32_t audio_worst_cycles(void);
uint32_t audio_active_voices(void);

/* The most voices that were ever mixed in one block. A two-second sample of
 * the instantaneous count lands between hits and reports zero. */
uint32_t audio_peak_voices(void);

/* Voices that were booked for a block already gone, so they sounded late.
 * Should be zero; anything else means the scheduler is not being called often
 * enough for its lookahead. */
uint32_t audio_late(void);

/* Bit per track, set while any of its voices is sounding. What the display
 * lights up, and cheap enough to read every frame. */
uint32_t audio_active_mask(void);

/* --- output capture, for null tests --------------------------------------- *
 *
 * Checksums the words the mixer emits over a fixed span of blocks. Armed rather
 * than started, because the mixer restarts track 0 and resets the sum in the
 * same block - two runs then see byte-identical input, and any difference in
 * the result is the thing under test rather than a block boundary landing
 * somewhere different. */
void audio_capture_arm(uint32_t blocks);
bool audio_capture_done(void);
uint32_t audio_capture_sum(void);

#endif /* AUDIO_H */
