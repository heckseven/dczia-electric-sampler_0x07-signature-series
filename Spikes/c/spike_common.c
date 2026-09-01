#include "spike_common.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "hardware/structs/systick.h"
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"
#include "pico/time.h"

/* The case names a spike may announce. Held as a table rather than a string in
 * a scratch register because there are only four registers, they are 32 bits
 * each, and an index survives a reset where a pointer does not. */
#define SPIKE_MAX_CASES 32
static const char *case_names[SPIKE_MAX_CASES];
static uint32_t case_count;
static uint32_t previous_case_index = 0xFFFFFFFFu;

static absolute_time_t last_heartbeat;
static bool heartbeat_required;

/* --- the scratch contract ------------------------------------------------ */

static uint32_t scratch_check(uint32_t index) {
    /* Anything that is not a plain copy. A stale register that happens to
     * contain a plausible index should not be read as one. */
    return index ^ SPIKE_SCRATCH_MAGIC;
}

static void scratch_store(uint32_t index) {
    watchdog_hw->scratch[SPIKE_SCRATCH_MAGIC_INDEX] = SPIKE_SCRATCH_MAGIC;
    watchdog_hw->scratch[SPIKE_SCRATCH_CASE_INDEX] = index;
    watchdog_hw->scratch[SPIKE_SCRATCH_CHECK_INDEX] = scratch_check(index);
}

static void scratch_clear(void) {
    watchdog_hw->scratch[SPIKE_SCRATCH_MAGIC_INDEX] = 0;
    watchdog_hw->scratch[SPIKE_SCRATCH_CASE_INDEX] = 0;
    watchdog_hw->scratch[SPIKE_SCRATCH_CHECK_INDEX] = 0;
}

static bool scratch_load(uint32_t *index) {
    if (watchdog_hw->scratch[SPIKE_SCRATCH_MAGIC_INDEX] != SPIKE_SCRATCH_MAGIC) {
        return false;
    }
    uint32_t stored = watchdog_hw->scratch[SPIKE_SCRATCH_CASE_INDEX];
    if (watchdog_hw->scratch[SPIKE_SCRATCH_CHECK_INDEX] != scratch_check(stored)) {
        return false;
    }
    *index = stored;
    return true;
}

/* --- reporting ------------------------------------------------------------ */

void spike_begin(const char *name, uint32_t version) {
    /* Read the scratch before anything else can overwrite it, then hold still
     * with USB up. If the previous image wedged during its own init, this
     * window is the only moment the host is guaranteed to reach us. */
    uint32_t stored;
    if (scratch_load(&stored)) {
        previous_case_index = stored;
    }
    scratch_clear();

    stdio_init_all();

    /* Listen through the hold - do not merely wait it out.
     *
     * This window exists so the host can always reach a freshly booted spike.
     * It could not: it slept. A spike that wedges before reaching its pump loop
     * is then unreachable by anything except holding BOOTSEL while replugging,
     * which is the single failure the whole recovery contract is meant to
     * prevent. Found by falling into it - a DMA abort spun forever, USB stopped
     * being serviced, and the badge had to be recovered by hand.
     */
    absolute_time_t hold_until = make_timeout_time_ms(SPIKE_CDC_WINDOW_MS);
    while (!time_reached(hold_until)) {
        if (getchar_timeout_us(1000) == 'B') {
            spike_reboot_to_bootsel();
        }
    }

    printf("SPIKE name=%s version=%lu reset=%s\n", name,
           (unsigned long)version,
           watchdog_caused_reboot() ? "watchdog" : "clean");

    watchdog_enable(SPIKE_WATCHDOG_MS, true);
    last_heartbeat = get_absolute_time();
    heartbeat_required = SPIKE_HEARTBEAT_MS > 0;
}

void spike_case(const char *name) {
    uint32_t index = case_count;
    if (index < SPIKE_MAX_CASES) {
        case_names[index] = name;
        case_count = index + 1;
    }
    scratch_store(index);
    printf("CASE case=%s state=STARTED\n", name);
}

void spike_case_state(const char *name, const char *state) {
    printf("CASE case=%s state=%s\n", name, state);
}

void spike_result(const char *name, const char *fmt, ...) {
    printf("RESULT case=%s ", name);
    va_list args;
    va_start(args, fmt);
    vprintf(fmt, args);
    va_end(args);
    printf("\n");
    /* A case that reported is a case that finished. Clearing here means a
     * later hang between cases is not blamed on the one that just passed. */
    scratch_clear();
}

void spike_done(const char *name) {
    scratch_clear();
    printf("DONE spike=%s\n", name);
}

const char *spike_previous_case(void) {
    if (previous_case_index >= case_count) {
        return NULL;
    }
    return case_names[previous_case_index];
}

void spike_report_previous(void) {
    /* Called after every case has been registered, so the index resolves.
     * A spike that died mid-case shows up here as HUNG and the run continues
     * from the next one, which is what makes the campaign unattended. */
    const char *name = spike_previous_case();
    if (name != NULL) {
        spike_case_state(name, "HUNG");
    }
}

/* --- staying reachable ---------------------------------------------------- */

void spike_pump(void) {
    watchdog_update();

    if (!heartbeat_required) {
        return;
    }
    /* Anything the host sends counts. It does not matter what: the question is
     * whether USB is still carrying traffic, not what the traffic says. */
    int c = getchar_timeout_us(0);
    while (c != PICO_ERROR_TIMEOUT) {
        if (c == 'B') {
            /* The host asking for BOOTSEL, so it can flash the next image or
             * put CircuitPython back. */
            spike_reboot_to_bootsel();
        }
        last_heartbeat = get_absolute_time();
        c = getchar_timeout_us(0);
    }

    if (absolute_time_diff_us(last_heartbeat, get_absolute_time()) >
        (int64_t)SPIKE_HEARTBEAT_MS * 1000) {
        /* USB is gone but the CPU is fine, so the watchdog will never fire and
         * the host cannot reach us. Reset deliberately: a reboot is
         * recoverable, a silent healthy badge is not.
         */
        printf("CASE case=heartbeat state=LOST\n");
        watchdog_reboot(0, 0, 0);
        while (true) {
            tight_loop_contents();
        }
    }
}

void spike_reboot_to_bootsel(void) {
    printf("CASE case=bootsel state=ENTERING\n");
    /* Flushed explicitly: the reset below does not drain the CDC buffer, and a
     * line the host never sees is a line that did not happen. */
    stdio_flush();
    sleep_ms(50);
    reset_usb_boot(0, 0);
}

/* --- SysTick as a cycle counter ------------------------------------------- */

void spike_cycles_begin(void) {
    systick_hw->csr = 0;          /* stop while reconfiguring */
    systick_hw->rvr = 0x00FFFFFF; /* full 24-bit reload */
    systick_hw->cvr = 0;          /* writing any value clears it */
    systick_hw->csr = 0x5;        /* enable, use the processor clock */
}

uint32_t spike_cycles_end(void) {
    /* Down-counting, so elapsed is reload minus current. */
    return 0x00FFFFFFu - (systick_hw->cvr & 0x00FFFFFFu);
}

bool spike_cycles_wrapped(uint32_t elapsed) {
    /* The COUNTFLAG bit latches a wrap and clears on read. At 133 MHz the
     * counter covers about 126 ms, so anything longer is unmeasurable this way
     * and must be reported as such rather than as a small number. */
    return (systick_hw->csr & (1u << 16)) != 0 || elapsed >= 0x00FFFFF0u;
}
