/* Task 7: what the display costs, and whether the pop is even digital.
 *
 * `sequencer.py` stops the I2S stream when nothing is sounding, and says why:
 *
 *     "An active stream makes the amplifier sensitive to traffic on the shared
 *      supply, and the display is on that supply: with the stream running,
 *      redrawing the screen pops audibly; with it stopped, the identical
 *      redrawing is silent. Confirmed on the badge."
 *
 * Confirmed by ear, which establishes that something pops - not where. If the
 * display perturbs the *digital* path, by stealing enough CPU to make a refill
 * late, then it is a software fault and fixable. If the output words are
 * bit-identical with the bus idle and with it hammering, the artefact is
 * downstream of everything software can reach, and the only remedies are
 * electrical or the stream-stopping the firmware already does.
 *
 * That distinction decides a real trade. Task 5 measured the amp's wake-up at
 * 7 ms typical, so stopping the stream to avoid the pop costs 7 ms on the next
 * hit - against a 2.56 ms trigger-to-output latency with the stream running.
 * The current firmware is paying nearly three times its whole latency budget to
 * avoid an artefact nobody has localised.
 *
 * So: a null test first, free, before any analog argument.
 *
 * Then the thing the plan actually wants timed. The 32 ms per frame in the
 * notes is a `displayio` figure, and the 400 kHz bus floor for 513 bytes is
 * about 11.5 ms - so most of that 32 ms is software, not the bus, and the two
 * need separating before anyone designs around either.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"

#include "i2s.pio.h"
#include "spike_common.h"

#define SPIKE_NAME "display"
#define SPIKE_VERSION 1

/* setup.py: busio.I2C(board.GP15, board.GP14) is (scl, sda), and GP14/GP15 are
 * I2C1. The panel is an SSD1306 128x32 at 0x3C. */
#define PIN_SDA 14
#define PIN_SCL 15
#define DISPLAY_I2C i2c1
#define DISPLAY_ADDR 0x3C

#define OLED_W 128
#define OLED_H 32
#define OLED_PAGES (OLED_H / 8)
#define OLED_BYTES (OLED_W * OLED_PAGES) /* 512 */

/* I2S, matching Task 5's setup so the two results compose. */
#define CLOCK_PIN 0
#define DATA_PIN 2
#define SAMPLE_RATE 16000
#define BITS_PER_FRAME 64
#define BLOCK_FRAMES 32

static uint32_t block_buffer[2][BLOCK_FRAMES];
static PIO pio = pio0;
static uint sm_out;
static int dma_a = -1, dma_b = -1;

/* The waveform is generated from a counter, so the same span of blocks always
 * produces the same words. That is what makes a checksum comparison meaningful:
 * any difference is the display's doing, not the signal's. */
static volatile uint32_t phase_counter;
static volatile uint32_t capture_remaining;
static volatile uint32_t capture_sum;
static volatile uint32_t capture_blocks;
static volatile uint32_t refill_last_us;
static volatile uint32_t refill_max_gap_us;
static volatile uint32_t refill_count;

/* --- audio, in SRAM ------------------------------------------------------- */

static void __not_in_flash_func(fill)(uint32_t *buffer) {
    uint32_t c = phase_counter;
    uint32_t sum = capture_sum;
    bool capturing = capture_remaining > 0;
    for (uint32_t i = 0; i < BLOCK_FRAMES; i++) {
        /* A quiet, deterministic ramp, capped at SPIKE_TEST_AMPLITUDE.
         *
         * The first version masked to 0x03FF - about -30 dBFS, four times
         * louder than the rest of the campaign and needlessly so. The test
         * needs the words to be reproducible, not loud: the checksum is just
         * as deterministic at a quarter of the level. */
        uint16_t s = (uint16_t)((c * 137u) & (SPIKE_TEST_AMPLITUDE - 1u));
        uint32_t word = ((uint32_t)s << 16) | s;
        buffer[i] = word;
        if (capturing) {
            /* Order-sensitive so a reordering or a repeat cannot cancel out. */
            sum = (sum * 31u) + word;
        }
        c++;
    }
    phase_counter = c;
    if (capturing) {
        capture_sum = sum;
        capture_blocks++;
        capture_remaining--;
    }
}

static void __not_in_flash_func(dma_handler)(void) {
    uint32_t now = timer_hw->timerawl;
    uint32_t gap = now - refill_last_us;
    if (refill_count != 0 && gap > refill_max_gap_us) {
        refill_max_gap_us = gap;
    }
    refill_last_us = now;
    refill_count++;

    if (dma_hw->ints0 & (1u << dma_a)) {
        dma_hw->ints0 = 1u << dma_a;
        fill(block_buffer[0]);
        dma_channel_hw_addr(dma_a)->read_addr = (uintptr_t)block_buffer[0];
        dma_channel_hw_addr(dma_a)->transfer_count = BLOCK_FRAMES;
    }
    if (dma_hw->ints0 & (1u << dma_b)) {
        dma_hw->ints0 = 1u << dma_b;
        fill(block_buffer[1]);
        dma_channel_hw_addr(dma_b)->read_addr = (uintptr_t)block_buffer[1];
        dma_channel_hw_addr(dma_b)->transfer_count = BLOCK_FRAMES;
    }
}

static void audio_stop(void) {
    /* Break the ping-pong chain before stopping, or each channel restarts the
     * one just aborted and the abort bit never clears. */
    hw_write_masked(&dma_channel_hw_addr(dma_a)->al1_ctrl,
                    (uint32_t)dma_a << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                    DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS);
    hw_write_masked(&dma_channel_hw_addr(dma_b)->al1_ctrl,
                    (uint32_t)dma_b << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                    DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS);
    while (dma_channel_is_busy(dma_a) || dma_channel_is_busy(dma_b)) {
        tight_loop_contents();
    }
    dma_channel_set_irq0_enabled(dma_a, false);
    dma_channel_set_irq0_enabled(dma_b, false);
    pio_sm_set_enabled(pio, sm_out, false);
}

static void audio_start(void) {
    phase_counter = 0;
    fill(block_buffer[0]);
    fill(block_buffer[1]);

    dma_channel_config a = dma_channel_get_default_config(dma_a);
    channel_config_set_transfer_data_size(&a, DMA_SIZE_32);
    channel_config_set_read_increment(&a, true);
    channel_config_set_write_increment(&a, false);
    channel_config_set_dreq(&a, pio_get_dreq(pio, sm_out, true));
    channel_config_set_chain_to(&a, dma_b);
    dma_channel_configure(dma_a, &a, &pio->txf[sm_out], block_buffer[0],
                          BLOCK_FRAMES, false);

    dma_channel_config b = dma_channel_get_default_config(dma_b);
    channel_config_set_transfer_data_size(&b, DMA_SIZE_32);
    channel_config_set_read_increment(&b, true);
    channel_config_set_write_increment(&b, false);
    channel_config_set_dreq(&b, pio_get_dreq(pio, sm_out, true));
    channel_config_set_chain_to(&b, dma_a);
    dma_channel_configure(dma_b, &b, &pio->txf[sm_out], block_buffer[1],
                          BLOCK_FRAMES, false);

    dma_hw->ints0 = (1u << dma_a) | (1u << dma_b);
    dma_channel_set_irq0_enabled(dma_a, true);
    dma_channel_set_irq0_enabled(dma_b, true);
    refill_count = 0;
    refill_max_gap_us = 0;
    refill_last_us = timer_hw->timerawl;

    pio_sm_set_enabled(pio, sm_out, true);
    dma_channel_start(dma_a);
}

/* --- the panel ------------------------------------------------------------ */

/* One DMA'd burst: a control byte then a payload, with STOP on the last entry.
 * RP2040's I2C takes 16-bit writes into DATA_CMD so the STOP and RESTART bits
 * travel with the byte, which is what lets a whole frame go out under DMA
 * instead of a byte at a time. */
static uint16_t i2c_cmds[OLED_BYTES + 2];
static int dma_i2c = -1;

static uint32_t i2c_burst(uint8_t control, const uint8_t *data, uint32_t len) {
    i2c_cmds[0] = control;
    for (uint32_t i = 0; i < len; i++) {
        i2c_cmds[i + 1] = data[i];
    }
    i2c_cmds[len] |= I2C_IC_DATA_CMD_STOP_BITS;

    DISPLAY_I2C->hw->enable = 0;
    DISPLAY_I2C->hw->tar = DISPLAY_ADDR;
    DISPLAY_I2C->hw->enable = 1;

    dma_channel_config c = dma_channel_get_default_config(dma_i2c);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_16);
    channel_config_set_read_increment(&c, true);
    channel_config_set_write_increment(&c, false);
    channel_config_set_dreq(&c, i2c_get_dreq(DISPLAY_I2C, true));
    dma_channel_configure(dma_i2c, &c, &DISPLAY_I2C->hw->data_cmd, i2c_cmds,
                          len + 1, true);

    /* CPU time is everything up to here; bus time is the wait. Split so the
     * "32 ms a frame" figure can be attributed rather than quoted. */
    uint32_t cpu_done = timer_hw->timerawl;
    dma_channel_wait_for_finish_blocking(dma_i2c);
    while ((DISPLAY_I2C->hw->status & I2C_IC_STATUS_TFE_BITS) == 0) {
        tight_loop_contents();
    }
    return cpu_done;
}

static void oled_command(uint8_t c) {
    uint8_t byte = c;
    i2c_burst(0x00, &byte, 1);
}

static void oled_init(void) {
    static const uint8_t seq[] = {
        0xAE,             /* off */
        0xD5, 0x80,       /* clock divide */
        0xA8, OLED_H - 1, /* multiplex */
        0xD3, 0x00,       /* offset */
        0x40,             /* start line 0 */
        0x8D, 0x14,       /* charge pump on */
        0x20, 0x00,       /* horizontal addressing */
        0xA1, 0xC8,       /* segment remap, COM scan dec */
        0xDA, 0x02,       /* COM pins, 128x32 */
        0x81, 0x8F,       /* contrast */
        0xD9, 0xF1, 0xDB, 0x40,
        0xA4, 0xA6,       /* resume, normal */
        0xAF,             /* on */
    };
    for (uint32_t i = 0; i < count_of(seq); i++) {
        oled_command(seq[i]);
    }
}

static void oled_window(uint8_t page_lo, uint8_t page_hi) {
    oled_command(0x21);
    oled_command(0x00);
    oled_command(OLED_W - 1);
    oled_command(0x22);
    oled_command(page_lo);
    oled_command(page_hi);
}

static uint8_t frame[OLED_BYTES];

/* Returns total microseconds; *cpu_us gets the part before the bus wait. */
static uint32_t oled_blit(uint32_t bytes, uint8_t pattern, uint32_t *cpu_us) {
    memset(frame, pattern, bytes);
    uint32_t t0 = timer_hw->timerawl;
    uint32_t cpu_done = i2c_burst(0x40, frame, bytes);
    uint32_t t1 = timer_hw->timerawl;
    *cpu_us = cpu_done - t0;
    return t1 - t0;
}

/* --- cases ---------------------------------------------------------------- */

/* Blocks to checksum per phase. 400 at 2 ms is 0.8 s, long enough to cover many
 * full-frame writes at either bus speed. */
#define CAPTURE_BLOCKS 400

static uint32_t run_capture(bool hammer) {
    capture_sum = 0;
    capture_blocks = 0;
    refill_max_gap_us = 0;
    refill_count = 0;
    refill_last_us = timer_hw->timerawl;
    phase_counter = 0;
    capture_remaining = CAPTURE_BLOCKS;

    while (capture_remaining > 0) {
        if (hammer) {
            uint32_t cpu;
            oled_window(0, OLED_PAGES - 1);
            oled_blit(OLED_BYTES, 0xAA, &cpu);
            oled_window(0, OLED_PAGES - 1);
            oled_blit(OLED_BYTES, 0x55, &cpu);
        }
        spike_pump();
    }
    return capture_sum;
}

static void case_null(void) {
    spike_case("null");

    audio_start();
    busy_wait_us(100000);

    uint32_t quiet = run_capture(false);
    uint32_t quiet_gap = refill_max_gap_us;

    uint32_t busy = run_capture(true);
    uint32_t busy_gap = refill_max_gap_us;

    spike_result("null",
                 "blocks=%d quiet_sum=0x%08lx busy_sum=0x%08lx identical=%d "
                 "quiet_gap_us=%lu busy_gap_us=%lu block_period_us=%d",
                 CAPTURE_BLOCKS, (unsigned long)quiet, (unsigned long)busy,
                 quiet == busy ? 1 : 0, (unsigned long)quiet_gap,
                 (unsigned long)busy_gap,
                 (BLOCK_FRAMES * 1000000) / SAMPLE_RATE);

    spike_case_state("null", "DONE");
}

static void case_frame(void) {
    spike_case("frame");

    static const uint32_t speeds[] = {400000, 1000000};
    for (uint32_t si = 0; si < count_of(speeds); si++) {
        uint32_t got = i2c_set_baudrate(DISPLAY_I2C, speeds[si]);

        /* Full frame, and one page - the partial-window update a sequencer
         * actually does when only a line of text changed. */
        static const uint32_t sizes[] = {OLED_BYTES, OLED_W};
        for (uint32_t zi = 0; zi < count_of(sizes); zi++) {
            uint32_t bytes = sizes[zi];
            uint32_t best = 0xFFFFFFFFu, worst = 0, cpu_worst = 0;
            uint64_t total = 0, cpu_total = 0;
            const uint32_t trials = 20;
            for (uint32_t t = 0; t < trials; t++) {
                uint32_t cpu = 0;
                oled_window(0, bytes == OLED_BYTES ? OLED_PAGES - 1 : 0);
                uint32_t us = oled_blit(bytes, t & 1 ? 0xAA : 0x55, &cpu);
                if (us < best) {
                    best = us;
                }
                if (us > worst) {
                    worst = us;
                }
                if (cpu > cpu_worst) {
                    cpu_worst = cpu;
                }
                total += us;
                cpu_total += cpu;
                spike_pump();
            }
            spike_result("frame",
                         "hz=%lu bytes=%lu min_us=%lu mean_us=%lu max_us=%lu "
                         "cpu_mean_us=%lu cpu_max_us=%lu trials=%lu",
                         (unsigned long)got, (unsigned long)bytes,
                         (unsigned long)best,
                         (unsigned long)(total / trials),
                         (unsigned long)worst,
                         (unsigned long)(cpu_total / trials),
                         (unsigned long)cpu_worst, (unsigned long)trials);
        }
    }

    spike_case_state("frame", "DONE");
}

int main(void) {
    spike_begin(SPIKE_NAME, SPIKE_VERSION);

    /* I2S first, so the display work happens against a live stream. */
    uint offset_out = pio_add_program(pio, &i2s_out_program);
    sm_out = pio_claim_unused_sm(pio, true);
    float div = (float)clock_get_hz(clk_sys) /
                (float)(SAMPLE_RATE * BITS_PER_FRAME);
    i2s_out_init(pio, sm_out, offset_out, CLOCK_PIN, DATA_PIN, div);
    dma_a = dma_claim_unused_channel(true);
    dma_b = dma_claim_unused_channel(true);
    irq_set_exclusive_handler(DMA_IRQ_0, dma_handler);
    irq_set_enabled(DMA_IRQ_0, true);

    i2c_init(DISPLAY_I2C, 400000);
    gpio_set_function(PIN_SDA, GPIO_FUNC_I2C);
    gpio_set_function(PIN_SCL, GPIO_FUNC_I2C);
    gpio_pull_up(PIN_SDA);
    gpio_pull_up(PIN_SCL);
    dma_i2c = dma_claim_unused_channel(true);

    oled_init();

    spike_result("identity", "sys_clk_hz=%lu oled=%dx%d bytes=%d addr=0x%02x",
                 (unsigned long)clock_get_hz(clk_sys), OLED_W, OLED_H,
                 OLED_BYTES, DISPLAY_ADDR);

    case_frame();
    case_null();

    /* Stop the stream before idling. Leaving it running means the badge sits
     * on a bench playing a test tone until somebody reflashes it. */
    audio_stop();

    spike_report_previous();
    spike_done(SPIKE_NAME);

    while (true) {
        spike_pump();
        sleep_ms(10);
    }
}
