/* Variant (a): the common base, and the proof that the round trip works.
 *
 * This is the first C image the badge runs, and flashing it removes
 * CircuitPython until it is put back. So before it measures anything it
 * establishes the two things the rest of the campaign depends on:
 *
 *   - the host can talk to a C image over CDC, and
 *   - the host can send it back to BOOTSEL, so CircuitPython can be restored
 *     without anybody holding a button.
 *
 * Then it reports the fixed SRAM overhead of a CDC-only build. Not "free SRAM":
 * free memory in a design that allocates nothing at runtime is just 264 KB
 * minus the things this reports minus the buffers a later phase chooses, and
 * asking for it here would mean pre-deciding those buffers.
 */

#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/structs/sio.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"

#include "spike_common.h"

#define SPIKE_NAME "scaffold"
#define SPIKE_VERSION 1

/* Symbols the linker script defines. Reading the map file gives the same
 * numbers, but reading them at runtime proves they describe the image that is
 * actually running rather than the one that was last built. */
extern char __data_start__, __data_end__;
extern char __bss_start__, __bss_end__;
extern char __HeapLimit, __StackLimit, __StackTop;
extern char __flash_binary_start, __flash_binary_end;

/* Core 1 does nothing yet and still costs a stack. Started anyway, because the
 * rewrite's whole premise is that audio gets its own core, and a scaffold that
 * never starts one would under-report by a stack. */
static volatile bool core1_alive;

static void core1_entry(void) {
    core1_alive = true;
    while (true) {
        tight_loop_contents();
    }
}

static void case_identity(void) {
    spike_case("identity");
    spike_result("identity",
                 "sys_clk_hz=%lu core1=%d",
                 (unsigned long)clock_get_hz(clk_sys),
                 core1_alive ? 1 : 0);
}

static void case_memory(void) {
    spike_case("memory");

    uint32_t data = (uint32_t)(&__data_end__ - &__data_start__);
    uint32_t bss = (uint32_t)(&__bss_end__ - &__bss_start__);
    uint32_t flash = (uint32_t)(&__flash_binary_end - &__flash_binary_start);

    /* The SDK's default layout puts both core stacks at the top of SRAM. The
     * gap between the heap limit and the stack limit is what is left for
     * everything a later phase wants to place. */
    uint32_t stack_total = (uint32_t)(&__StackTop - &__StackLimit);
    uint32_t gap = (uint32_t)(&__StackLimit - &__HeapLimit);

    spike_result("memory",
                 "variant=cdc data=%lu bss=%lu stack=%lu gap=%lu flash=%lu",
                 (unsigned long)data,
                 (unsigned long)bss,
                 (unsigned long)stack_total,
                 (unsigned long)gap,
                 (unsigned long)flash);
}

static void case_systick(void) {
    spike_case("systick");

    /* A known-length busy wait, to check the cycle counter reads sensibly
     * before anything is measured with it. One millisecond at 133 MHz should
     * be about 133,000 cycles; what matters is the order of magnitude and that
     * it did not wrap. */
    spike_cycles_begin();
    busy_wait_us(1000);
    uint32_t elapsed = spike_cycles_end();

    spike_result("systick",
                 "us=1000 cycles=%lu wrapped=%d sys_clk_hz=%lu",
                 (unsigned long)elapsed,
                 spike_cycles_wrapped(elapsed) ? 1 : 0,
                 (unsigned long)clock_get_hz(clk_sys));
}

int main(void) {
    spike_begin(SPIKE_NAME, SPIKE_VERSION);

    multicore_launch_core1(core1_entry);
    sleep_ms(10);

    case_identity();
    case_memory();
    case_systick();

    /* Every case is registered by now, so a hang on the previous boot can be
     * named rather than merely counted. */
    spike_report_previous();
    spike_done(SPIKE_NAME);

    /* Stay reachable. The host ends the session by sending 'B', which is what
     * puts CircuitPython back within reach - so this loop is the return path,
     * not an idle. */
    while (true) {
        spike_pump();
        sleep_ms(10);
    }
}
