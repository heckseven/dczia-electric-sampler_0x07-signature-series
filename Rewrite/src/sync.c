/* See sync.h. */

#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "board.h"
#include "sync.h"

/* --- input ----------------------------------------------------------------- */

/* Edges arrive in an interrupt and are consumed by the main loop, so a ring
 * rather than a single slot: a burst at 24 PPQN is a pulse every 20 ms, but the
 * main loop can be inside an SD write for thirty. Eight is four times the worst
 * backlog that has been measured, and dropping the oldest would lose the very
 * measurement the clock is waiting for. */
#define PULSE_RING 8

static volatile uint32_t pulse_at[PULSE_RING];
static volatile uint32_t pulse_head; /* written by the interrupt */
static volatile uint32_t pulse_tail; /* read by the main loop */
static volatile uint32_t pulse_count;
static volatile uint32_t pulse_rejected;
static volatile uint32_t last_edge_us;

static void __not_in_flash_func(sync_in_isr)(void) {
    /* In RAM: this fires while the main loop may be blocked on flash, and an
     * XIP miss here would put a variable delay into the one timestamp whose
     * whole value is that it has none. */
    gpio_acknowledge_irq(PIN_SYNC_IN, GPIO_IRQ_EDGE_FALL);

    uint32_t now = timer_hw->timerawl;
    /* Debounce in time rather than by re-reading the pin. A jack is a long
     * unshielded wire and the first edge is the true one; waiting to confirm it
     * would throw away the accuracy this whole path exists for. */
    if (last_edge_us != 0 && (now - last_edge_us) < SYNC_MIN_GAP_US) {
        pulse_rejected++;
        return;
    }
    last_edge_us = now;

    uint32_t next = (pulse_head + 1u) % PULSE_RING;
    if (next == pulse_tail) {
        /* Full. The main loop has not drained in eight pulses, which is a
         * stall worth seeing rather than papering over by overwriting. */
        pulse_rejected++;
        return;
    }
    pulse_at[pulse_head] = now;
    pulse_head = next;
    pulse_count++;
}

bool sync_take_pulse(uint32_t *at_us) {
    if (pulse_tail == pulse_head) {
        return false;
    }
    *at_us = pulse_at[pulse_tail];
    __asm volatile("dmb" ::: "memory");
    pulse_tail = (pulse_tail + 1u) % PULSE_RING;
    return true;
}

uint32_t sync_pulses_in(void) {
    return pulse_count;
}

uint32_t sync_pulses_rejected(void) {
    return pulse_rejected;
}

/* --- output ---------------------------------------------------------------- */

/* One alarm serves both edges: it raises the pin, then re-arms itself for the
 * fall. A second pulse cannot be booked while one is in flight, which at the
 * fastest rate this firmware offers is not a situation the jack can be in. */
static int alarm_num = -1;
static volatile bool pulse_high;
static volatile uint32_t wanted_us;
static volatile uint32_t worst_error_us;
static volatile uint32_t pulses_out;
static volatile uint32_t pulses_late;

/* The callback is installed once, in sync_init, and never cleared. Clearing and
 * re-installing it from inside itself - which the first version did, to switch
 * between the two edges - races with a caller booking the next pulse. Which
 * edge this is, is state, not a different callback. */
static void __not_in_flash_func(sync_out_alarm)(uint alarm) {
    (void)alarm;
    if (!pulse_high) {
        gpio_put(PIN_SYNC_OUT, 1);
        pulse_high = true;

        /* How far the edge actually landed from where it was asked for.
         *
         * The point of scheduling this against audio_frame_time_us rather than
         * sending it when the tick is processed is that it arrives with the
         * beat. That claim is worth measuring rather than asserting, and it
         * costs one subtraction on a path that runs twice a beat. */
        uint32_t error = timer_hw->timerawl - wanted_us;
        if (error < 1000000u && error > worst_error_us) {
            worst_error_us = error;
        }
        pulses_out++;

        if (hardware_alarm_set_target(
                alarm_num,
                from_us_since_boot(timer_hw->timerawl + SYNC_PULSE_US))) {
            /* Already past, so nothing will lower the pin. Do it now rather
             * than leave the jack stuck high. */
            gpio_put(PIN_SYNC_OUT, 0);
            pulse_high = false;
        }
    } else {
        gpio_put(PIN_SYNC_OUT, 0);
        pulse_high = false;
    }
}

void sync_pulse_at_frame(uint64_t frame) {
    if (alarm_num < 0 || pulse_high) {
        return;
    }
    uint32_t when_us;
    if (!audio_frame_time_us(frame, &when_us)) {
        return; /* nothing has played yet, so there is no beat to align to */
    }

    wanted_us = when_us;
    if (hardware_alarm_set_target(alarm_num, from_us_since_boot(when_us))) {
        /* The beat has already been and gone. A pulse now would be heard as
         * the *next* beat arriving early, which is worse than a missing one -
         * a dropped pulse is a gap, a wrong one is a wrong tempo. */
        pulses_late++;
    }
}

uint32_t sync_pulses_out(void) {
    return pulses_out;
}

uint32_t sync_pulses_missed(void) {
    return pulses_late;
}

uint32_t sync_out_worst_error_us(void) {
    return worst_error_us;
}

/* --- setup ----------------------------------------------------------------- */

void sync_init(void) {
    gpio_init(PIN_SYNC_OUT);
    gpio_set_dir(PIN_SYNC_OUT, GPIO_OUT);
    gpio_put(PIN_SYNC_OUT, 0);

    /* Idles high and is pulled down for each pulse, so the event is a falling
     * edge - see the note in sequencer.py's _poll_sync_in. */
    gpio_init(PIN_SYNC_IN);
    gpio_set_dir(PIN_SYNC_IN, GPIO_IN);
    gpio_pull_up(PIN_SYNC_IN);
    gpio_add_raw_irq_handler(PIN_SYNC_IN, sync_in_isr);
    gpio_set_irq_enabled(PIN_SYNC_IN, GPIO_IRQ_EDGE_FALL, true);
    irq_set_enabled(IO_IRQ_BANK0, true);

    if (alarm_num < 0) {
        alarm_num = (int)hardware_alarm_claim_unused(true);
        hardware_alarm_set_callback(alarm_num, sync_out_alarm);
    }
}
