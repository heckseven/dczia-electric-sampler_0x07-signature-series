/* Phase 1 criterion 2: trigger-to-output, measured at the pin.
 *
 * Task 5 measured this on a purpose-built rig and got block x (1..2) + 568 us.
 * This runs the same measurement through the *shipped* audio path - the real
 * mixer, the real voice allocation, the real ping-pong - because a number taken
 * from a spike says what the spike does, not what the instrument does.
 *
 * The two mistakes Task 5 documented are avoided the same way:
 *
 *   The trigger instant is dithered across the block period. A fixed phase
 *   returns one number repeated and min/mean/worst collapse into a degenerate
 *   result wearing a distribution's clothes.
 *
 *   The timestamp comes from the pin, not the DMA transfer count. The count
 *   says a frame entered the TX FIFO; the FIFO is eight words deep, which is
 *   half a millisecond of the answer at 16 kHz.
 */

#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/pio.h"
#include "hardware/structs/systick.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "console.h"
#include "watch.pio.h"

/* Quiet, and the mixer's gain chain is accounted for below rather than assumed
 * away: this drives a real speaker. */
#define TEST_VALUE 0x0100
#define TRIALS 300

static PIO pio = pio0;

int main(void) {
    console_begin("rt-latency");
    audio_init();

    /* A constant sample, so the very first frame of the block carries the value
     * and the watcher has something to see immediately. */
    uint32_t frames = SAMPLE_RATE / 4;
    int16_t *tone = audio_arena_alloc(frames);
    for (uint32_t i = 0; i < frames; i++) {
        tone[i] = TEST_VALUE;
    }
    audio_set_sample(0, tone, frames);
    audio_set_pitch(0, PITCH_UNITY);
    audio_set_gain(0, 0x7FFF);
    audio_set_master(0x7FFF);

    /* What the mixer will actually emit, after the per-voice and master gains.
     * The watcher fires on the first HIGH bit, so the answer is early by
     * however many bit periods separate the MSB from the highest set bit -
     * a known, exact offset rather than something to wave away. */
    int32_t emitted = ((TEST_VALUE * 0x7FFF) >> 15);
    emitted = (emitted * 0x7FFF) >> 15;
    uint32_t top = 0;
    for (uint32_t b = 0; b < 16; b++) {
        if (emitted & (1 << b)) {
            top = b;
        }
    }
    uint32_t offset_bits = 15 - top;
    uint32_t bit_ns = 1000000000u / (SAMPLE_RATE * 32u);
    uint32_t correction_us = (offset_bits * bit_ns) / 1000u;

    /* The watcher shares pio0 with the I2S program audio_init() added. */
    uint offset = pio_add_program(pio, &data_watch_program);
    uint sm_watch = pio_claim_unused_sm(pio, true);
    float div = (float)clock_get_hz(clk_sys) /
                (float)(SAMPLE_RATE * I2S_BITS_PER_FRAME);
    data_watch_init(pio, sm_watch, offset, PIN_I2S_DATA, div);
    pio_sm_set_enabled(pio, sm_watch, true);

    audio_start();

    uint32_t period = (BLOCK_FRAMES * 1000000u) / SAMPLE_RATE;
    uint32_t clk_mhz = clock_get_hz(clk_sys) / 1000000u;

    printf("RESULT case=setup block_frames=%d period_us=%lu emitted=%ld "
           "offset_bits=%lu correction_us=%lu trials=%d\n",
           BLOCK_FRAMES, (unsigned long)period, (long)emitted,
           (unsigned long)offset_bits, (unsigned long)correction_us, TRIALS);

    uint32_t best = 0xFFFFFFFFu, worst = 0, counted = 0, lost = 0;
    uint64_t total = 0;

    systick_hw->rvr = 0x00FFFFFF;
    systick_hw->cvr = 0;
    systick_hw->csr = 0x5;

    for (uint32_t t = 0; t < TRIALS; t++) {
        /* Let the pin fall quiet, or the watcher fires on the tail of the
         * previous trial rather than the start of this one. */
        audio_stop_all();
        busy_wait_us(period * 4);
        pio_interrupt_clear(pio, 0);

        /* Walk the trigger across the block period. */
        busy_wait_us((t * period) / TRIALS);

        uint32_t start = systick_hw->cvr;
        audio_trigger(0);
        uint32_t guard = 0;
        while (!pio_interrupt_get(pio, 0)) {
            if (++guard > 1000000u) {
                break;
            }
        }
        uint32_t cycles = (start - systick_hw->cvr) & 0x00FFFFFFu;

        if (guard > 1000000u) {
            lost++;
        } else {
            uint32_t us = cycles / clk_mhz;
            us = us > correction_us ? us - correction_us : 0;
            if (us < best) {
                best = us;
            }
            if (us > worst) {
                worst = us;
            }
            total += us;
            counted++;
        }
        console_pump();
    }

    audio_stop_all();
    printf("RESULT case=latency min_us=%lu mean_us=%lu max_us=%lu trials=%lu "
           "lost=%lu underruns=%lu\n",
           (unsigned long)(counted ? best : 0),
           (unsigned long)(counted ? total / counted : 0),
           (unsigned long)worst, (unsigned long)counted, (unsigned long)lost,
           (unsigned long)audio_underruns());
    printf("DONE spike=rt-latency\n");

    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
