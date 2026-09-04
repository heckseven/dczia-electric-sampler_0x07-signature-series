/* The two MIDI ports. The parser they feed is in midi.c, which has no
 * hardware in it and is tested on the host - see tests/test_midi.c. */

#include "hardware/gpio.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "song.h"
#include "board.h"
#include "midi.h"

#define MIDI_UART uart1
#define MIDI_BAUD 31250

static struct midi_parser uart_parser;
static uint32_t bytes_in;
static uint32_t bytes_out;


bool midi_receive(struct midi_message *out) {
    while (uart_is_readable(MIDI_UART)) {
        uint8_t byte = uart_getc(MIDI_UART);
        bytes_in++;
        if (midi_parse(&uart_parser, byte, out)) {
            return true;
        }
    }
    return false;
}

static void send(const uint8_t *bytes, uint32_t length) {
    /* Non-blocking by choice. A MIDI cable with nothing on the other end still
     * clocks out at 31250 baud, but a receiver holding the line - or a jack
     * with nothing plugged into it - must not be able to stall the sequencer.
     * Dropping a clock byte is a glitch; blocking the main loop is an
     * underrun. */
    for (uint32_t i = 0; i < length; i++) {
        if (!uart_is_writable(MIDI_UART)) {
            return;
        }
        uart_putc_raw(MIDI_UART, (char)bytes[i]);
        bytes_out++;
    }
}

/* When each sounding note should be lifted, in milliseconds since boot. Zero
 * means the note is not sounding. Indexed by track rather than by note number:
 * this firmware only ever sends the eight it owns, and a 128-entry table would
 * be 120 entries of nothing. */
static uint32_t note_off_due[TRACK_COUNT];

void midi_send_note_on(uint8_t note, uint8_t velocity) {
    uint8_t message[3] = {(uint8_t)(0x90u | MIDI_CHANNEL), note, velocity};
    send(message, 3);

    int32_t track = (int32_t)note - MIDI_NOTE_BASE;
    if (track >= 0 && track < TRACK_COUNT) {
        /* Retriggering restarts the hold rather than lifting the old note
         * first. A drum part hitting the same pad twice quickly should read as
         * two hits on the receiver, and an off between them would make the
         * second one a retrigger of something already stopping. */
        note_off_due[track] = to_ms_since_boot(get_absolute_time()) +
                              MIDI_NOTE_HOLD_MS;
    }
}

void midi_pump(void) {
    uint32_t now = to_ms_since_boot(get_absolute_time());
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        if (note_off_due[t] != 0 &&
            (int32_t)(now - note_off_due[t]) >= 0) {
            note_off_due[t] = 0;
            midi_send_note_off((uint8_t)(MIDI_NOTE_BASE + t));
        }
    }
}

void midi_send_note_off(uint8_t note) {
    /* Note on at velocity zero rather than 0x80. Both are legal; this one can
     * share running status with the note on that preceded it, which is what
     * most gear sends and what most gear is tested against. */
    uint8_t message[3] = {(uint8_t)(0x90u | MIDI_CHANNEL), note, 0};
    send(message, 3);
}

/* --- clock out, scheduled --------------------------------------------------- */

#define CLOCK_QUEUE 8

static int clock_alarm = -1;
static volatile uint32_t clock_at_us[CLOCK_QUEUE];
static volatile uint32_t clock_head, clock_tail;
static volatile uint32_t clocks_sent, clocks_dropped;

static void arm_next_clock(void);

static void __not_in_flash_func(clock_alarm_fired)(uint alarm) {
    (void)alarm;
    uint8_t byte = 0xF8u;
    /* Straight to the register. This is an interrupt and `send` counts bytes
     * and checks writability; a clock byte that cannot go out now is one that
     * should be dropped rather than deferred, because a late clock is worse
     * than a missing one. */
    if (uart_is_writable(MIDI_UART)) {
        uart_get_hw(MIDI_UART)->dr = byte;
        clocks_sent++;
    } else {
        clocks_dropped++;
    }
    clock_tail = (clock_tail + 1u) % CLOCK_QUEUE;
    arm_next_clock();
}

static void arm_next_clock(void) {
    if (clock_tail == clock_head) {
        return;
    }
    if (hardware_alarm_set_target(
            clock_alarm, from_us_since_boot(clock_at_us[clock_tail]))) {
        /* Already past: fire it now rather than leaving the queue stuck
         * behind a target that will never arrive. */
        clock_alarm_fired((uint)clock_alarm);
    }
}

void midi_clock_at_frame(uint64_t frame) {
    if (clock_alarm < 0) {
        return;
    }
    uint32_t when_us;
    if (!audio_frame_time_us(frame, &when_us)) {
        return;
    }
    uint32_t next = (clock_head + 1u) % CLOCK_QUEUE;
    if (next == clock_tail) {
        clocks_dropped++;
        return;
    }
    bool was_idle = (clock_tail == clock_head);
    clock_at_us[clock_head] = when_us;
    clock_head = next;
    if (was_idle) {
        arm_next_clock();
    }
}

uint32_t midi_clocks_sent(void) {
    return clocks_sent;
}

uint32_t midi_clocks_dropped(void) {
    return clocks_dropped;
}

void midi_send_clock(void) {
    uint8_t byte = 0xF8u;
    send(&byte, 1);
}

void midi_send_start(void) {
    uint8_t byte = 0xFAu;
    send(&byte, 1);
}

void midi_send_stop(void) {
    uint8_t byte = 0xFCu;
    send(&byte, 1);
}

uint32_t midi_bytes_in(void) {
    return bytes_in;
}

uint32_t midi_bytes_out(void) {
    return bytes_out;
}

/* Placed last so the clock queue above is already declared: the alarm is
 * claimed here and its callback lives up there. */
void midi_init(void) {
    uart_init(MIDI_UART, MIDI_BAUD);
    gpio_set_function(PIN_MIDI_TX, GPIO_FUNC_UART);
    gpio_set_function(PIN_MIDI_RX, GPIO_FUNC_UART);
    /* No flow control and no FIFO interrupt: the stream is drained from the
     * main loop, and at 31250 baud the 32-byte hardware FIFO holds ten
     * milliseconds of the densest traffic MIDI can carry. */
    uart_set_hw_flow(MIDI_UART, false, false);
    uart_set_format(MIDI_UART, 8, 1, UART_PARITY_NONE);

    if (clock_alarm < 0) {
        clock_alarm = (int)hardware_alarm_claim_unused(true);
        hardware_alarm_set_callback(clock_alarm, clock_alarm_fired);
    }
}
