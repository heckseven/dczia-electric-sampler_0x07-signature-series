/* The fault matrix: does the recovery contract actually catch each way a spike
 * can die?
 *
 * Everything else in this campaign rests on this. An unattended run assumes a
 * wedged badge comes back by itself, and so far exactly two legs have been
 * exercised - the heartbeat, by accident, and the watchdog, by falling into a
 * DMA abort spin. Both were found the hard way, and one of them only worked
 * after `pause_on_debug` was turned off. That is not a contract, that is two
 * anecdotes.
 *
 * Three ways to die, each needing a different mechanism to survive:
 *
 *   interrupts_off   __disable_irq() then spin. The watchdog is a hardware
 *                    counter on its own clock, so masking interrupts must not
 *                    save the firmware from it. If this leg fails, nothing that
 *                    disables interrupts is ever safe to run unattended.
 *
 *   hard_fault       execute `udf #0`. The default handler spins forever with
 *                    interrupts still on, which the watchdog should also catch -
 *                    but by a different path, and a fault handler that resets
 *                    the chip before the watchdog would hide the difference.
 *
 *   usb_dead         stop servicing USB while staying healthy. The CPU is fine
 *                    and feeding the dog, so the watchdog will never fire; only
 *                    the host-silence heartbeat turns this into a reset. This is
 *                    the leg the plan's critique said a watchdog alone misses.
 *
 * The matrix drives itself. Each boot reads the next leg from a scratch
 * register, reports what the previous one did, then injects. Nobody has to be
 * watching, which is the point.
 */

#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/irq.h"
#include "hardware/structs/watchdog.h"
#include "hardware/sync.h"
#include "hardware/watchdog.h"
#include "pico/stdlib.h"

#include "spike_common.h"

#define SPIKE_NAME "fault"
#define SPIKE_VERSION 1

/* Scratch 3. Registers 0-2 carry the in-flight case, and 4-7 belong to the SDK's
 * watchdog-reboot magic - writing those manufactures a boot loop that never
 * enumerates, which is the one failure this campaign cannot recover from. */
#define FAULT_SCRATCH_INDEX 3
#define FAULT_MAGIC 0xFA017000u
#define FAULT_MASK 0x000000FFu

enum {
    LEG_INTERRUPTS_OFF = 0,
    LEG_HARD_FAULT,
    LEG_USB_DEAD,
    LEG_COUNT,
};

static const char *LEG_NAMES[LEG_COUNT] = {
    "interrupts_off",
    "hard_fault",
    "usb_dead",
};

/* Which mechanism each leg is supposed to be caught by, so the report says what
 * was expected as well as what happened. */
static const char *LEG_EXPECTS[LEG_COUNT] = {
    "watchdog",
    "watchdog",
    "heartbeat",
};

static uint32_t leg_load(void) {
    uint32_t stored = watchdog_hw->scratch[FAULT_SCRATCH_INDEX];
    if ((stored & ~FAULT_MASK) != FAULT_MAGIC) {
        return 0;
    }
    uint32_t index = stored & FAULT_MASK;
    return index > LEG_COUNT ? 0 : index;
}

static void leg_store(uint32_t index) {
    watchdog_hw->scratch[FAULT_SCRATCH_INDEX] = FAULT_MAGIC | (index & FAULT_MASK);
}

static void inject(uint32_t leg) {
    switch (leg) {
    case LEG_INTERRUPTS_OFF:
        /* `cpsid i` rather than a CMSIS wrapper: the SDK does not expose
         * __disable_irq here, and the instruction is what the wrapper would
         * emit anyway. Masking interrupts must not save the firmware from a
         * watchdog that runs on its own clock. */
        __asm volatile("cpsid i" ::: "memory");
        while (true) {
            tight_loop_contents();
        }

    case LEG_HARD_FAULT:
        /* Permanently undefined, by definition of the encoding. Escalates to
         * HardFault, whose default handler spins. */
        __asm volatile("udf #0");
        while (true) {
            tight_loop_contents();
        }

    case LEG_USB_DEAD:
        /* Healthy in every way the watchdog can see: the loop runs, the dog is
         * fed, and the chip will happily do this forever. Only the absence of
         * host traffic distinguishes it from working. */
        irq_set_enabled(USBCTRL_IRQ, false);
        while (true) {
            watchdog_update();
            spike_pump();
            sleep_ms(10);
        }

    default:
        break;
    }
}

int main(void) {
    uint32_t leg = leg_load();

    spike_begin(SPIKE_NAME, SPIKE_VERSION);

    bool by_watchdog = watchdog_caused_reboot();
    spike_result("matrix",
                 "boot leg=%lu reset=%s sys_clk_hz=%lu",
                 (unsigned long)leg, by_watchdog ? "watchdog" : "clean",
                 (unsigned long)clock_get_hz(clk_sys));

    /* Leg N's verdict is read on boot N+1: the previous leg was injected, and
     * the fact that this code is running at all is the recovery. */
    if (leg > 0) {
        uint32_t previous = leg - 1;
        spike_result("matrix",
                     "verdict leg=%s expected=%s recovered=%d reset=%s",
                     LEG_NAMES[previous], LEG_EXPECTS[previous],
                     by_watchdog ? 1 : 0,
                     by_watchdog ? "watchdog" : "clean");
    }

    if (leg >= LEG_COUNT) {
        /* Clear, so a later run of this image starts the matrix over rather
         * than reporting done forever. */
        watchdog_hw->scratch[FAULT_SCRATCH_INDEX] = 0;
        spike_result("matrix", "complete legs=%d", LEG_COUNT);
        spike_report_previous();
        spike_done(SPIKE_NAME);
        while (true) {
            spike_pump();
            sleep_ms(10);
        }
    }

    spike_result("matrix", "injecting leg=%s expected=%s",
                 LEG_NAMES[leg], LEG_EXPECTS[leg]);

    /* Advance BEFORE injecting: whatever happens next, the following boot must
     * move on rather than repeat the same fault forever. */
    leg_store(leg + 1);

    /* Give the host a moment to read the line above - after this, the only
     * evidence of what happened is that the badge came back. */
    sleep_ms(300);

    inject(leg);

    /* Unreachable unless a leg failed to fault, which is itself a result. */
    spike_result("matrix", "leg=%s DID NOT FAULT", LEG_NAMES[leg]);
    while (true) {
        spike_pump();
        sleep_ms(10);
    }
}
