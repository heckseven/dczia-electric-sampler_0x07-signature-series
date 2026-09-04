/* The sync jacks: one pulse out, one pulse in.
 *
 * This is where the rewrite's clock is worth having. engine/clock.py carries a
 * long apparatus for averaging tempo across a window of pulses, and its own
 * comments say why: `ticks_ms` gives whole milliseconds, so at 24 PPQN and 120
 * BPM the gap between pulses is 20.8 ms and a single millisecond of error is
 * nearly five percent. It measured 131 to 156 BPM for a steady 137.6.
 *
 * Here an incoming edge is timestamped in its own interrupt off the 1 MHz
 * hardware timer, so the same measurement is a thousand times finer and does
 * not depend on where the main loop happened to be. Averaging is still done -
 * a master's own jitter is real - but it is smoothing a good measurement
 * rather than rescuing a coarse one.
 *
 * Outgoing pulses are scheduled against audio_frame_time_us rather than emitted
 * when the tick is processed. The sequencer books ticks up to sixteen
 * milliseconds ahead so it can hand each hit to the mixer on an exact frame;
 * sending the pulse at that moment would put it sixteen milliseconds early. The
 * pulse has to leave when the beat is *heard*.
 */

#ifndef SYNC_H
#define SYNC_H

#include <stdbool.h>
#include <stdint.h>

/* Matches SYNC_PULSE_MS in sequencer.py. Long enough for the gear this drives
 * to see it, short enough to fit inside a tick at the top of the tempo range:
 * 24 PPQN at 300 BPM is a tick every 8.3 ms. */
#define SYNC_PULSE_US 5000

/* Pulses closer together than this are contact noise or a reflection, not
 * music. 2 ms is engine/clock.py's MIN_PULSE_MS. */
#define SYNC_MIN_GAP_US 2000

void sync_init(void);

/* Send a pulse when `frame` reaches the pin. Ignored if the audio clock has not
 * started, or if a pulse is still going out - at the fastest rate this firmware
 * offers the gap is 8.3 ms against a 5 ms pulse, so an overlap means the caller
 * asked for something the jack cannot express. */
void sync_pulse_at_frame(uint64_t frame);

/* Take the next captured input edge, in microseconds on the hardware timer.
 * False when there is none waiting. */
bool sync_take_pulse(uint32_t *at_us);

/* Edges seen and edges dropped for arriving too close together, for the stats
 * line - a cable that is chattering shows up here rather than as a tempo that
 * will not settle. */
uint32_t sync_pulses_in(void);
uint32_t sync_pulses_rejected(void);

/* The output side, measured rather than asserted.
 *
 * `sync_out_worst_error_us` is how far the worst outgoing edge landed from
 * where it was asked for. Scheduling a pulse for when the beat is *heard* is
 * the whole reason this goes through an alarm instead of being set when the
 * tick is processed, and that claim should be checkable on the badge with
 * nothing plugged into the jack. */
uint32_t sync_pulses_out(void);
uint32_t sync_pulses_missed(void);
uint32_t sync_out_worst_error_us(void);

#endif /* SYNC_H */
