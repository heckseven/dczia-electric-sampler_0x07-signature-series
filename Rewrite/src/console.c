/* Staying reachable, and coming back when something goes wrong.
 *
 * Carried over from the Phase 0 harness, which earned every line of it:
 *
 *   - The boot window LISTENS, it does not merely wait. A window that holds
 *     still without reading the one command that returns to BOOTSEL is only a
 *     pause, and a firmware that wedges before its main loop is then reachable
 *     only by holding the button.
 *
 *   - pause_on_debug is false. True sets PAUSE_DBG0, PAUSE_DBG1 and PAUSE_JTAG,
 *     any of which halts the counter while the debug interface looks active -
 *     and this badge has exposed SWD pads with nothing driving them. With it
 *     true, a wedged badge sat enumerated-but-dead for over an hour with an 8 s
 *     watchdog armed. With it false, the fault matrix recovered from a spin
 *     with interrupts masked, a hard fault, and a dead USB stack.
 */

#include <stdio.h>

#include "hardware/watchdog.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"

#include "console.h"

void console_begin(const char *name) {
    stdio_init_all();

    absolute_time_t until = make_timeout_time_ms(CONSOLE_WINDOW_MS);
    while (!time_reached(until)) {
        if (getchar_timeout_us(1000) == 'B') {
            reset_usb_boot(0, 0);
        }
    }

    printf("BOOT name=%s reset=%s\n", name,
           watchdog_caused_reboot() ? "watchdog" : "clean");
    watchdog_enable(CONSOLE_WATCHDOG_MS, false);
}

void console_pump(void) {
    watchdog_update();
    int c = getchar_timeout_us(0);
    while (c != PICO_ERROR_TIMEOUT) {
        if (c == 'B') {
            reset_usb_boot(0, 0);
        }
        c = getchar_timeout_us(0);
    }
}
