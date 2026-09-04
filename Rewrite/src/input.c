/* See input.h. */

#include <string.h>

#include "hardware/gpio.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"

#include "input.h"

/* A small ring. Twelve keys and two encoders cannot generate more than a
 * handful of events between polls, and dropping the oldest is the right failure
 * for an instrument: the newest hit is the one the player just made. */
#define QUEUE_SIZE 32

static struct input_event queue[QUEUE_SIZE];
static uint32_t queue_head, queue_tail;

static bool key_down[KEY_COUNT];
static uint32_t key_locked_until[KEY_COUNT];

/* Quadrature, two bits of history against two bits of present. The four
 * impossible transitions - both signals changing at once - decode to zero,
 * which is how a missed sample degrades into a lost step rather than a step in
 * the wrong direction. */
static const int8_t QUADRATURE[16] = {0, -1, 1,  0, 1, 0, 0, -1,
                                      -1, 0, 0,  1, 0, 1, -1, 0};

struct encoder {
    uint8_t pin_a;
    uint8_t pin_b;
    uint8_t state;
    int32_t subcounts; /* four per detent on the encoders this badge uses */
};

static struct encoder select_enc = {PIN_ENC_SELECT_A, PIN_ENC_SELECT_B, 0, 0};
static struct encoder volume_enc = {PIN_ENC_VOLUME_A, PIN_ENC_VOLUME_B, 0, 0};

static void push(enum input_event_kind kind, uint8_t key, int32_t delta) {
    uint32_t next = (queue_head + 1) % QUEUE_SIZE;
    if (next == queue_tail) {
        /* Full. Drop the oldest, so the newest hit always survives. */
        queue_tail = (queue_tail + 1) % QUEUE_SIZE;
    }
    queue[queue_head].kind = kind;
    queue[queue_head].key = key;
    queue[queue_head].delta = delta;
    queue_head = next;
}

static void encoder_init(struct encoder *e) {
    gpio_init(e->pin_a);
    gpio_set_dir(e->pin_a, GPIO_IN);
    gpio_pull_up(e->pin_a);
    gpio_init(e->pin_b);
    gpio_set_dir(e->pin_b, GPIO_IN);
    gpio_pull_up(e->pin_b);
    e->state = (uint8_t)((gpio_get(e->pin_a) << 1) | gpio_get(e->pin_b));
}

static int32_t encoder_poll(struct encoder *e) {
    uint8_t now = (uint8_t)((gpio_get(e->pin_a) << 1) | gpio_get(e->pin_b));
    e->state = (uint8_t)(((e->state << 2) | now) & 0x0F);
    e->subcounts += QUADRATURE[e->state];

    /* Detents only. Reporting every quarter-step would make one click of the
     * knob move a setting four times. */
    int32_t detents = e->subcounts / 4;
    if (detents != 0) {
        e->subcounts -= detents * 4;
    }
    return detents;
}

void input_init(void) {
    queue_head = queue_tail = 0;
    memset(key_down, 0, sizeof(key_down));
    memset(key_locked_until, 0, sizeof(key_locked_until));

    /* setup.py builds the matrix with columns_to_anodes=False, which puts the
     * diode anodes on the rows: current flows row to column. So a row is driven
     * HIGH to scan it and the columns are pulled DOWN, with a pressed key
     * reading HIGH. Getting this backwards reads every key as permanently
     * pressed, which is at least loud enough to notice. */
    for (uint32_t r = 0; r < KEY_ROWS; r++) {
        gpio_init(PIN_KEY_ROWS[r]);
        gpio_set_dir(PIN_KEY_ROWS[r], GPIO_OUT);
        gpio_put(PIN_KEY_ROWS[r], 0);
    }
    for (uint32_t c = 0; c < KEY_COLS; c++) {
        gpio_init(PIN_KEY_COLS[c]);
        gpio_set_dir(PIN_KEY_COLS[c], GPIO_IN);
        gpio_pull_down(PIN_KEY_COLS[c]);
    }

    encoder_init(&select_enc);
    encoder_init(&volume_enc);
}

bool input_read_key_now(uint8_t key) {
    if (key >= KEY_COUNT) {
        return false;
    }
    uint32_t row = key / KEY_COLS;
    uint32_t col = key % KEY_COLS;
    gpio_put(PIN_KEY_ROWS[row], 1);
    busy_wait_us(3);
    bool pressed = gpio_get(PIN_KEY_COLS[col]);
    gpio_put(PIN_KEY_ROWS[row], 0);
    return pressed;
}

void input_poll(void) {
    uint32_t now = timer_hw->timerawl / 1000u; /* milliseconds */

    for (uint32_t r = 0; r < KEY_ROWS; r++) {
        gpio_put(PIN_KEY_ROWS[r], 1);
        /* The row line has to settle before the columns mean anything. A few
         * microseconds is generous for a board this size, and the whole scan
         * still costs well under a tenth of a millisecond. */
        busy_wait_us(3);

        for (uint32_t c = 0; c < KEY_COLS; c++) {
            uint32_t key = r * KEY_COLS + c;
            bool pressed = gpio_get(PIN_KEY_COLS[c]);

            if (pressed == key_down[key]) {
                continue;
            }
            if ((int32_t)(now - key_locked_until[key]) < 0) {
                continue; /* still inside the bounce window */
            }

            key_down[key] = pressed;
            key_locked_until[key] = now + KEY_LOCKOUT_MS;
            push(pressed ? INPUT_KEY_DOWN : INPUT_KEY_UP, (uint8_t)key, 0);
        }

        gpio_put(PIN_KEY_ROWS[r], 0);
    }

    int32_t select_delta = encoder_poll(&select_enc);
    if (select_delta != 0) {
        push(INPUT_SELECT_TURN, 0, select_delta);
    }
    int32_t volume_delta = encoder_poll(&volume_enc);
    if (volume_delta != 0) {
        push(INPUT_VOLUME_TURN, 0, volume_delta);
    }
}

bool input_next(struct input_event *event) {
    if (queue_tail == queue_head) {
        return false;
    }
    *event = queue[queue_tail];
    queue_tail = (queue_tail + 1) % QUEUE_SIZE;
    return true;
}

bool input_held(uint8_t key) {
    return key < KEY_COUNT && key_down[key];
}
