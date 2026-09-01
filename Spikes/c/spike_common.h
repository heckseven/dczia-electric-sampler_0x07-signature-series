/* Shared scaffolding for every C spike.
 *
 * Three jobs, none of them measurement:
 *
 *   1. Speak the Spikes report format, so one host parser reads every spike.
 *   2. Survive a spike wedging the badge, and say afterwards which case was in
 *      flight when it died - because nobody is watching, and a campaign that
 *      loses its place on the first hang is not unattended.
 *   3. Get back to CircuitPython on demand, so the host can end a C session
 *      without anyone holding BOOTSEL.
 *
 * On the cycle counter: Cortex-M0+ has no DWT CYCCNT. SysTick at clk_sys is the
 * only cycle source, it counts DOWN, and it is 24 bits - about 126 ms at
 * 133 MHz before it wraps. Every timed section has to be shorter than that, and
 * anything longer has to be timed some other way.
 */

#ifndef SPIKE_COMMON_H
#define SPIKE_COMMON_H

#include <stdbool.h>
#include <stdint.h>

/* How long to hold still at boot before touching any peripheral.
 *
 * This is the campaign's guaranteed way back in. If a spike wedges the badge
 * during its own measurements, the next image still gets a window where USB is
 * up and nothing else has run yet - so the host can always reach a freshly
 * booted spike even when the previous one died. Without it, a spike that hangs
 * during peripheral init is only recoverable by hand.
 */
#define SPIKE_CDC_WINDOW_MS 3000

/* Scratch registers 0..3 only.
 *
 * The SDK reserves WATCHDOG_SCRATCH4..7 for its own watchdog-reboot magic - a
 * magic word plus an entry point, stack pointer and parameter. Writing a case
 * index there makes the next watchdog reset jump to a garbage vector, which is
 * a boot loop that never enumerates and so cannot be reached by the host at
 * all. The recovery mechanism must not be able to manufacture the one failure
 * it cannot recover from.
 */
#define SPIKE_SCRATCH_MAGIC_INDEX 0
#define SPIKE_SCRATCH_CASE_INDEX 1
#define SPIKE_SCRATCH_CHECK_INDEX 2
#define SPIKE_SCRATCH_MAGIC 0x5D1CE500u

/* Watchdog period. Long enough that a slow-but-working case is not killed,
 * short enough that a wedged one does not cost the campaign minutes. */
#define SPIKE_WATCHDOG_MS 8000

/* How long the host may go silent before the badge resets itself.
 *
 * The watchdog catches a hang with the CPU alive. It does NOT catch USB dying
 * while the main loop keeps feeding it - a badge that is healthy, silent, and
 * never resets, which the host cannot reach because the reset path it would
 * use is the USB that is gone. Requiring a periodic heartbeat turns that into
 * a reset, which is recoverable.
 *
 * Zero disables it, for a spike that must run longer than the host is willing
 * to talk to it.
 */
#define SPIKE_HEARTBEAT_MS 30000

void spike_begin(const char *name, uint32_t version);
void spike_case(const char *name);
void spike_case_state(const char *name, const char *state);
void spike_done(const char *name);

/* Printf-style, prefixed with `RESULT case=<name> `. */
void spike_result(const char *name, const char *fmt, ...);

/* Which case was in flight when the chip last died, or NULL if the last boot
 * was clean. Read once at startup, before anything overwrites it. */
const char *spike_previous_case(void);
void spike_report_previous(void);

/* Keep the dog fed and the heartbeat honest. Call from the case loop. */
void spike_pump(void);

/* Back to CircuitPython without anyone touching the badge. */
void spike_reboot_to_bootsel(void);

/* SysTick as a cycle counter. Down-counting, 24-bit, wraps at ~126 ms. */
void spike_cycles_begin(void);
uint32_t spike_cycles_end(void);
bool spike_cycles_wrapped(uint32_t elapsed);

#endif /* SPIKE_COMMON_H */
