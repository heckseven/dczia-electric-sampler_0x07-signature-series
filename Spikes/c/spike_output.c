/* Task 5: how long it takes a triggered sample to reach the pin, and whether
 * writing flash while audio is playing breaks it.
 *
 * These are the two numbers that decide the rewrite's audio architecture, and
 * neither can be argued from a datasheet:
 *
 *   (a) Trigger-to-output. The user's budget is under 10 ms. A double-buffered
 *       design pays up to two block periods before a newly triggered sample
 *       reaches the amp, so the block size is the latency, and the question is
 *       how small it can be before the refill cannot keep up.
 *
 *       Two things this deliberately does NOT do, both because they produce a
 *       confident wrong answer:
 *
 *       - Trigger at a fixed phase. A self-trigger at a fixed offset from the
 *         DMA completion sits at a fixed point in the block, so every trial
 *         returns the same number and min/mean/worst collapse into one value
 *         wearing a distribution's clothes. The trigger instant is dithered
 *         across the whole block period instead.
 *
 *       - Time the DMA transfer count. That says when a frame entered the PIO
 *         TX FIFO, not when it left the chip. A second state machine watches
 *         the data pin and raises an IRQ on the first high bit, which is a
 *         hardware timestamp of the sample actually leaving.
 *
 *   (b) The flash hazard. Erasing or programming flash stops XIP, so any code
 *       still living in flash - including interrupt handlers - stalls for the
 *       duration. If that is tens of milliseconds it dwarfs every other
 *       latency term in the design, and it would hurt a C rewrite exactly as
 *       much as it hurts the current firmware. The audio path here is forced
 *       into SRAM with __not_in_flash_func and every other interrupt is masked
 *       for the operation, which is what a rewrite would have to do anyway.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/flash.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"

#include "i2s.pio.h"
#include "spike_common.h"

#define SPIKE_NAME "output"
#define SPIKE_VERSION 1

#define CLOCK_PIN 0 /* GP0 BCLK, GP1 LRCLK */
#define DATA_PIN 2  /* GP2 */

#define SAMPLE_RATE 16000
#define BITS_PER_FRAME 64 /* PIO cycles per stereo frame, 2 per bit */
#define BITS_PER_BCLK 32  /* actual bit clocks per stereo frame */

#define MAX_BLOCK 128
static uint32_t block_buffer[2][MAX_BLOCK];

/* The value a triggered sample carries.
 *
 * Two constraints pull in opposite directions. The watcher fires on the first
 * HIGH bit, so the sooner a set bit appears in the word the less correction is
 * needed - which argues for 0x8000, whose sign bit is the very first bit out.
 * But this drives a real speaker on the badge, and 0x8000 is full scale.
 *
 * 0x0100 is -42 dBFS, quiet enough to be a tick rather than a bang, and its
 * highest set bit is position 7 counting from the MSB. That is a known, exact
 * offset of 7 bit periods, subtracted in the reported figure rather than
 * hand-waved.
 */
#define TRIGGER_VALUE SPIKE_TEST_AMPLITUDE
#define TRIGGER_BIT_OFFSET 7

/* 300, not the 10,000 the plan asked for. At the largest block a trial costs
 * about four block periods, and 10,000 of them would run the spike far past the
 * host-silence heartbeat. 300 per block size resolves the distribution well
 * enough to see whether it is flat, and the reduction is stated rather than
 * quietly applied. */
#define TRIALS 300

static PIO pio = pio0;
static uint sm_out, sm_watch;
static uint offset_out, offset_watch;

static int dma_a = -1, dma_b = -1;

static volatile bool sounding;
static volatile uint32_t current_block = 32;

/* Refill bookkeeping, all read after the fact so nothing prints from the IRQ. */
static volatile uint32_t refill_count;
static volatile uint32_t refill_last_us;
static volatile uint32_t refill_max_gap_us;

/* --- the audio path, all of which must live in SRAM ----------------------- *
 *
 * __not_in_flash_func is the whole point of case (b): during a flash erase the
 * XIP window is gone, and a handler in flash would fetch garbage or fault. This
 * is also why the handler calls nothing - `timer_hw->timerawl` is a register
 * read rather than time_us_32(), which is a function that lives in flash.
 */

static void __not_in_flash_func(fill)(uint32_t *buffer, uint32_t frames) {
    uint32_t value = sounding ? TRIGGER_VALUE : 0u;
    /* Left and right carry the same sample: the badge is mono, and the amp
     * takes one channel of a stereo frame. */
    uint32_t word = (value << 16) | value;
    for (uint32_t i = 0; i < frames; i++) {
        buffer[i] = word;
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

    /* Whichever channel just finished is the one whose buffer is now free.
     * Rewind it through the plain read_addr and transfer_count registers, not
     * their _trig aliases: the partner's chain is what restarts this channel,
     * and triggering it here as well would run two transfers into one FIFO. */
    if (dma_hw->ints0 & (1u << dma_a)) {
        dma_hw->ints0 = 1u << dma_a;
        fill(block_buffer[0], current_block);
        dma_channel_hw_addr(dma_a)->read_addr = (uintptr_t)block_buffer[0];
        dma_channel_hw_addr(dma_a)->transfer_count = current_block;
    }
    if (dma_hw->ints0 & (1u << dma_b)) {
        dma_hw->ints0 = 1u << dma_b;
        fill(block_buffer[1], current_block);
        dma_channel_hw_addr(dma_b)->read_addr = (uintptr_t)block_buffer[1];
        dma_channel_hw_addr(dma_b)->transfer_count = current_block;
    }
}

/* --- setup ---------------------------------------------------------------- */

static void audio_start(uint32_t frames) {
    current_block = frames;
    sounding = false;
    fill(block_buffer[0], frames);
    fill(block_buffer[1], frames);

    dma_channel_config a = dma_channel_get_default_config(dma_a);
    channel_config_set_transfer_data_size(&a, DMA_SIZE_32);
    channel_config_set_read_increment(&a, true);
    channel_config_set_write_increment(&a, false);
    channel_config_set_dreq(&a, pio_get_dreq(pio, sm_out, true));
    channel_config_set_chain_to(&a, dma_b);
    dma_channel_configure(dma_a, &a, &pio->txf[sm_out], block_buffer[0], frames,
                          false);

    dma_channel_config b = dma_channel_get_default_config(dma_b);
    channel_config_set_transfer_data_size(&b, DMA_SIZE_32);
    channel_config_set_read_increment(&b, true);
    channel_config_set_write_increment(&b, false);
    channel_config_set_dreq(&b, pio_get_dreq(pio, sm_out, true));
    channel_config_set_chain_to(&b, dma_a);
    dma_channel_configure(dma_b, &b, &pio->txf[sm_out], block_buffer[1], frames,
                          false);

    dma_hw->ints0 = (1u << dma_a) | (1u << dma_b);
    dma_channel_set_irq0_enabled(dma_a, true);
    dma_channel_set_irq0_enabled(dma_b, true);

    refill_count = 0;
    refill_max_gap_us = 0;
    refill_last_us = timer_hw->timerawl;

    pio_sm_set_enabled(pio, sm_out, true);
    pio_sm_set_enabled(pio, sm_watch, true);
    dma_channel_start(dma_a);
}

static void audio_stop(void) {
    /* Break the ping-pong before stopping it. Aborting a channel its partner
     * chains to lets the partner restart it, and the abort bit never clears -
     * the failure that wedged the badge during Task 4. */
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
    pio_sm_set_enabled(pio, sm_watch, false);
}

/* --- (a) trigger to output ------------------------------------------------ */

static uint32_t block_period_us(uint32_t frames) {
    return (frames * 1000000u) / SAMPLE_RATE;
}

static void case_latency(void) {
    spike_case("latency");

    static const uint32_t sizes[] = {16, 32, 64, 128};

    for (uint32_t si = 0; si < count_of(sizes); si++) {
        uint32_t frames = sizes[si];
        uint32_t period = block_period_us(frames);
        audio_start(frames);

        uint32_t best = 0xFFFFFFFFu, worst = 0;
        uint64_t total = 0;
        uint32_t counted = 0, lost = 0;

        for (uint32_t t = 0; t < TRIALS; t++) {
            /* Let the pin fall quiet, or the watcher fires on the tail of the
             * previous trial rather than the start of this one. */
            sounding = false;
            busy_wait_us(period * 3);
            pio_interrupt_clear(pio, 0);

            /* Walk the trigger across the block period, so the result is a
             * distribution rather than one number repeated. */
            busy_wait_us((t * period) / TRIALS);

            spike_cycles_begin();
            sounding = true;
            uint32_t guard = 0;
            while (!pio_interrupt_get(pio, 0)) {
                if (++guard > 1000000u) {
                    break;
                }
            }
            uint32_t cycles = spike_cycles_end();

            if (guard > 1000000u || spike_cycles_wrapped(cycles)) {
                lost++;
                continue;
            }
            if (cycles < best) {
                best = cycles;
            }
            if (cycles > worst) {
                worst = cycles;
            }
            total += cycles;
            counted++;

            /* Feed the dog inside the loop, not just between block sizes. At
             * 128 frames a trial costs about 48 ms, so 300 of them run 14 s -
             * comfortably past the 8 s watchdog, which reset the badge mid-case
             * the first time this ran. */
            if ((t & 15) == 0) {
                spike_pump();
            }
        }
        sounding = false;
        uint32_t gap = refill_max_gap_us;
        audio_stop();

        uint32_t clk_mhz = clock_get_hz(clk_sys) / 1000000u;
        uint32_t mean = counted ? (uint32_t)(total / counted) : 0;
        /* Subtract the known serialisation offset of the trigger value's first
         * set bit, so the figure is "frame reached the pin", not "bit 7 did". */
        uint32_t correction = (TRIGGER_BIT_OFFSET * 1000000u) /
                              (SAMPLE_RATE * BITS_PER_BCLK);

        spike_result("latency",
                     "frames=%lu period_us=%lu min_us=%lu mean_us=%lu "
                     "max_us=%lu correction_us=%lu trials=%lu lost=%lu "
                     "max_refill_gap_us=%lu",
                     (unsigned long)frames,
                     (unsigned long)period,
                     (unsigned long)(counted ? best / clk_mhz - correction : 0),
                     (unsigned long)(counted ? mean / clk_mhz - correction : 0),
                     (unsigned long)(counted ? worst / clk_mhz - correction : 0),
                     (unsigned long)correction,
                     (unsigned long)counted,
                     (unsigned long)lost,
                     (unsigned long)gap);
        spike_pump();
    }

    spike_case_state("latency", "DONE");
}

/* --- (b) the flash hazard -------------------------------------------------- */

/* The last 4 KB sector of the 2 MB part. Nothing may be erased without checking
 * this against the linked image first: an erase landing in the loaded firmware
 * bricks the only badge into a state whose one recovery is holding BOOTSEL
 * while replugging, which is the single thing this campaign cannot ask for. */
#define FLASH_TARGET_OFFSET (2 * 1024 * 1024 - FLASH_SECTOR_SIZE)

extern char __flash_binary_end;

static uint8_t flash_source[FLASH_PAGE_SIZE];

static void __not_in_flash_func(erase_and_program)(void) {
    flash_range_erase(FLASH_TARGET_OFFSET, FLASH_SECTOR_SIZE);
    flash_range_program(FLASH_TARGET_OFFSET, flash_source, FLASH_PAGE_SIZE);
}

static void case_flash(void) {
    spike_case("flash");

    uintptr_t target = XIP_BASE + FLASH_TARGET_OFFSET;
    uintptr_t image_end = (uintptr_t)&__flash_binary_end;
    if (target < image_end) {
        spike_result("flash",
                     "REFUSED target=0x%08lx image_end=0x%08lx",
                     (unsigned long)target, (unsigned long)image_end);
        spike_case_state("flash", "DONE");
        return;
    }
    spike_result("flash", "target=0x%08lx image_end=0x%08lx sector=%lu",
                 (unsigned long)target, (unsigned long)image_end,
                 (unsigned long)FLASH_SECTOR_SIZE);

    for (uint32_t i = 0; i < FLASH_PAGE_SIZE; i++) {
        flash_source[i] = (uint8_t)(i ^ 0x5A);
    }

    static const uint32_t sizes[] = {16, 32, 64, 128};
    for (uint32_t si = 0; si < count_of(sizes); si++) {
        uint32_t frames = sizes[si];
        uint32_t period = block_period_us(frames);
        audio_start(frames);
        sounding = true;

        /* Settle, and learn what the refill gap looks like with nothing else
         * happening. Without this baseline a gap during the erase cannot be
         * attributed to the erase. */
        busy_wait_us(200000);
        uint32_t quiet_gap = refill_max_gap_us;

        refill_max_gap_us = 0;
        uint32_t before = refill_count;

        /* Mask every interrupt except the audio refill. Everything else -
         * USB above all - has its handler in flash, and would fault the moment
         * XIP goes away. This is exactly the trade a rewrite has to make. */
        uint32_t saved = 0;
        for (uint32_t irq = 0; irq < 26; irq++) {
            if (irq_is_enabled(irq)) {
                saved |= 1u << irq;
            }
        }
        irq_set_mask_enabled(saved & ~(1u << DMA_IRQ_0), false);

        uint32_t t0 = timer_hw->timerawl;
        erase_and_program();
        uint32_t elapsed = timer_hw->timerawl - t0;

        irq_set_mask_enabled(saved, true);

        uint32_t gap = refill_max_gap_us;
        uint32_t refills = refill_count - before;
        sounding = false;
        audio_stop();

        /* An underrun is a refill that arrived later than the block it was
         * feeding needed it. The DMA keeps running either way and simply
         * replays whatever is in the buffer, so this is the only way to see it
         * without listening. */
        uint32_t underruns = gap > period ? 1 : 0;

        spike_result("flash",
                     "frames=%lu period_us=%lu erase_us=%lu quiet_gap_us=%lu "
                     "stall_gap_us=%lu refills=%lu underran=%lu",
                     (unsigned long)frames,
                     (unsigned long)period,
                     (unsigned long)elapsed,
                     (unsigned long)quiet_gap,
                     (unsigned long)gap,
                     (unsigned long)refills,
                     (unsigned long)underruns);
        spike_pump();
    }

    spike_case_state("flash", "DONE");
}

int main(void) {
    spike_begin(SPIKE_NAME, SPIKE_VERSION);

    offset_out = pio_add_program(pio, &i2s_out_program);
    offset_watch = pio_add_program(pio, &data_watch_program);
    sm_out = pio_claim_unused_sm(pio, true);
    sm_watch = pio_claim_unused_sm(pio, true);

    /* 64 PIO cycles per stereo frame, two per bit. 125 MHz / 1.024 MHz is
     * 122.0703125, which 16.8 fixed point holds exactly. */
    float div = (float)clock_get_hz(clk_sys) /
                (float)(SAMPLE_RATE * BITS_PER_FRAME);
    i2s_out_init(pio, sm_out, offset_out, CLOCK_PIN, DATA_PIN, div);
    data_watch_init(pio, sm_watch, offset_watch, DATA_PIN, div);

    dma_a = dma_claim_unused_channel(true);
    dma_b = dma_claim_unused_channel(true);
    irq_set_exclusive_handler(DMA_IRQ_0, dma_handler);
    irq_set_enabled(DMA_IRQ_0, true);

    spike_result("identity", "sys_clk_hz=%lu rate=%d clkdiv_milli=%lu",
                 (unsigned long)clock_get_hz(clk_sys), SAMPLE_RATE,
                 (unsigned long)(div * 1000.0f));

    case_latency();
    case_flash();

    spike_report_previous();
    spike_done(SPIKE_NAME);

    while (true) {
        spike_pump();
        sleep_ms(10);
    }
}
