/* Task 4: what a fixed-point mixer costs on this chip, and whether the bus
 * cares where its samples live.
 *
 * The planning arithmetic guessed ~10 cycles per voice-frame and concluded the
 * mixer was about 2% of one core - i.e. that determinism, not throughput, was
 * the problem. That guess assumed an instruction set this chip does not have.
 * Cortex-M0+ has no SMULL, no saturating arithmetic, no free operand shifts,
 * and only r0-r7 for most instructions. So the number is measured here rather
 * than argued.
 *
 * Three things are varied:
 *
 *   voices          1..24. The current firmware runs 18 (8 tracks x 2, plus an
 *                   audition and a metronome).
 *
 *   interpolation   off, and linear. Off is a plain gain-and-sum. Linear adds a
 *                   fractional phase accumulator, which on M0+ costs a second
 *                   load, a subtract, a second multiply and a shift - the term
 *                   the "5% with interpolation" estimate understated.
 *
 *   placement       samples in the main striped SRAM, and in SCRATCH_Y. On
 *                   RP2040, SRAM0-3 are interleaved every 4 bytes into one
 *                   256 KB region, so "a different striped bank" is not a
 *                   thing you can choose - the meaningful choice is striped
 *                   versus one of the two 4 KB scratch banks, which sit on
 *                   their own bus ports and do not contend with it.
 *
 * And each is run twice: quiet, and against a DMA hammering the striped SRAM.
 * That is the contention term, which does not show up as an interrupt and so
 * was missing from a plan whose whole thesis is determinism.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "pico/stdlib.h"

#include "spike_common.h"

#define SPIKE_NAME "mixer"
#define SPIKE_VERSION 4

#define MAX_VOICES 24
#define BLOCK_FRAMES 32
#define SAMPLE_FRAMES 512

/* Sample data, one buffer shared by every voice - the cache and bus behaviour
 * is what is being measured, not whether the voices play different sounds. */
static int16_t samples_striped[SAMPLE_FRAMES];

/* SCRATCH_Y is 4 KB and, in the SDK's default layout, otherwise unused: core1's
 * stack is in SCRATCH_X. 512 frames of 16-bit is 1 KB, so it fits. */
static int16_t samples_scratch[SAMPLE_FRAMES]
    __attribute__((section(".scratch_y.samples")));

static int32_t mix_buffer[BLOCK_FRAMES];

/* Somewhere for the contention DMA to write. 4 KB and 4 KB-aligned, because
 * the DMA wraps its write address with a ring rather than running off the end
 * of memory, and a ring's buffer must be aligned to its own size. */
#define CHURN_BYTES 4096
static uint8_t churn_sink[CHURN_BYTES] __attribute__((aligned(CHURN_BYTES)));

struct voice {
    const int16_t *data;
    uint32_t phase;      /* 16.16 fixed point into the sample */
    uint32_t step;       /* 16.16 per output frame */
    int16_t gain;        /* Q15 */
};

static struct voice voices[MAX_VOICES];

/* Two channels, not one. On RP2040 a channel whose CHAIN_TO names itself has
 * chaining DISABLED - so the obvious "chain to me" self-restart runs the
 * transfer exactly once and stops. The first version of this spike did that and
 * measured a churn that was over in about a thousand cycles against a
 * three-million-cycle window, which looks exactly like "contention is free".
 * A pair that chain to each other actually runs continuously. */
static int churn_a = -1;
static int churn_b = -1;

/* --- the two inner loops -------------------------------------------------- */

/* Plain gain-and-sum at unity rate. The cheapest thing that is still a mixer. */
static void __attribute__((noinline))
mix_plain(struct voice *v, uint32_t count, int32_t *out) {
    memset(out, 0, sizeof(int32_t) * BLOCK_FRAMES);
    for (uint32_t i = 0; i < count; i++) {
        const int16_t *data = v[i].data;
        int32_t gain = v[i].gain;
        uint32_t phase = v[i].phase >> 16;
        for (uint32_t f = 0; f < BLOCK_FRAMES; f++) {
            out[f] += ((int32_t)data[(phase + f) & (SAMPLE_FRAMES - 1)] * gain) >> 15;
        }
        v[i].phase += (uint32_t)BLOCK_FRAMES << 16;
    }
}

/* Linear interpolation, which is what any pitch other than 1.0 needs. The extra
 * work per frame is the second load, the subtract, the second multiply and the
 * shift - all of which are separate instructions on M0+. */
static void __attribute__((noinline))
mix_linear(struct voice *v, uint32_t count, int32_t *out) {
    memset(out, 0, sizeof(int32_t) * BLOCK_FRAMES);
    for (uint32_t i = 0; i < count; i++) {
        const int16_t *data = v[i].data;
        int32_t gain = v[i].gain;
        uint32_t phase = v[i].phase;
        uint32_t step = v[i].step;
        for (uint32_t f = 0; f < BLOCK_FRAMES; f++) {
            uint32_t index = (phase >> 16) & (SAMPLE_FRAMES - 1);
            uint32_t frac = (phase >> 8) & 0xFF;
            int32_t a = data[index];
            int32_t b = data[(index + 1) & (SAMPLE_FRAMES - 1)];
            int32_t s = a + (((b - a) * (int32_t)frac) >> 8);
            out[f] += (s * gain) >> 15;
            phase += step;
        }
        v[i].phase = phase;
    }
}

/* --- contention ----------------------------------------------------------- *
 *
 * One channel, no chaining, a very long transfer count, and a write ring. Two
 * earlier designs are worth recording because both produced a confident wrong
 * answer rather than an error:
 *
 *   - A single channel chained to itself. On RP2040, CHAIN_TO naming the
 *     channel's own number DISABLES chaining, so it ran 1,024 words - about a
 *     thousand cycles against a three-million-cycle window - and contention
 *     looked free.
 *   - A pair chained to each other. That runs continuously, but stopping it
 *     does not work: aborting a channel that another channel chains to lets
 *     the partner restart it, and `dma_hw->abort` never clears. That wedged
 *     the badge hard enough that the host could not reach it.
 *
 * A single unchained channel has neither problem. It reads one fixed word and
 * writes it round a 4 KB ring, so the bus sees a continuous write stream into
 * the striped SRAM and the transfer simply ends when aborted.
 */

/* 2^28 - 1 transfers, the largest the count field holds. At roughly one
 * transfer per cycle that is about two seconds, comfortably longer than any
 * measurement here, so the churn never runs out mid-window. */
#define CHURN_TRANSFERS 0x0FFFFFFFu

static const uint32_t churn_pattern = 0xA5A5A5A5u;
static int churn_channel = -1;

static void churn_start(void) {
    if (churn_channel < 0) {
        churn_channel = dma_claim_unused_channel(true);
    }
    dma_channel_config c = dma_channel_get_default_config(churn_channel);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_32);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, true);
    /* Wrap the write address inside churn_sink. 12 means 2^12 = 4096 bytes. */
    channel_config_set_ring(&c, true, 12);
    /* No DREQ: flat out. Harsher than the real I2S stream this stands in for -
     * 16 kHz mono is one transfer every 7,812 cycles - so a null result here is
     * a strong null and a positive one is an upper bound. */
    dma_channel_configure(churn_channel, &c, churn_sink, &churn_pattern,
                          CHURN_TRANSFERS, true);
}

/* Was the churn still running at the end of the window? Without this a
 * contention result cannot be told apart from a DMA that quietly stopped -
 * which is the mistake the first version of this spike made. */
static uint32_t churn_witness(void) {
    return (churn_channel >= 0 && dma_channel_is_busy(churn_channel)) ? 1 : 0;
}

static void churn_stop(void) {
    if (churn_channel < 0) {
        return;
    }
    dma_channel_abort(churn_channel);
}

/* --- measurement ---------------------------------------------------------- */

static void prepare(const int16_t *data, uint32_t count, uint32_t step) {
    for (uint32_t i = 0; i < count; i++) {
        voices[i].data = data;
        voices[i].phase = (uint32_t)(i * 7) << 16; /* scattered starts */
        voices[i].step = step;
        voices[i].gain = 0x2000;
    }
}

/* Blocks per measurement. Enough to be well above the 30 us the host clock can
 * resolve, and short enough to stay inside SysTick's 134 ms at 125 MHz. */
#define BLOCKS 200

static uint32_t time_blocks(void (*fn)(struct voice *, uint32_t, int32_t *),
                            uint32_t count) {
    spike_cycles_begin();
    for (uint32_t b = 0; b < BLOCKS; b++) {
        fn(voices, count, mix_buffer);
    }
    return spike_cycles_end();
}

static void run_one(const char *interp, const char *placement, int contended,
                    const int16_t *data, uint32_t count, uint32_t step,
                    void (*fn)(struct voice *, uint32_t, int32_t *)) {
    prepare(data, count, step);
    if (contended) {
        churn_start();
    }
    uint32_t cycles = time_blocks(fn, count);
    uint32_t remaining = contended ? churn_witness() : 0;
    if (contended) {
        churn_stop();
    }

    uint32_t wrapped = spike_cycles_wrapped(cycles) ? 1 : 0;
    /* Per voice-frame, which is the unit the planning arithmetic guessed at. */
    uint32_t voice_frames = BLOCKS * BLOCK_FRAMES * count;
    uint32_t centi_cycles = voice_frames ? (cycles * 100u) / voice_frames : 0;

    /* Fraction of one core needed to sustain 16 kHz, in tenths of a percent.
     * cycles per block / (clk / (16000 / BLOCK_FRAMES)) */
    uint32_t clk = clock_get_hz(clk_sys);
    uint32_t cycles_per_block = cycles / BLOCKS;
    uint32_t cycles_available = clk / (16000u / BLOCK_FRAMES);
    uint32_t permille = (uint32_t)(((uint64_t)cycles_per_block * 1000u) /
                                   cycles_available);

    spike_result("mix",
                 "interp=%s place=%s contended=%d voices=%lu "
                 "cycles_per_block=%lu centicycles_per_voiceframe=%lu "
                 "permille_of_core=%lu churn_busy=%lu wrapped=%lu",
                 interp, placement, contended, (unsigned long)count,
                 (unsigned long)cycles_per_block,
                 (unsigned long)centi_cycles,
                 (unsigned long)permille,
                 (unsigned long)remaining,
                 (unsigned long)wrapped);
}

static void case_mix(void) {
    spike_case("mix");

    static const uint32_t counts[] = {1, 4, 8, 18, 24};

    for (uint32_t ci = 0; ci < count_of(counts); ci++) {
        uint32_t n = counts[ci];
        run_one("none", "striped", 0, samples_striped, n, 1u << 16, mix_plain);
        run_one("linear", "striped", 0, samples_striped, n, 0x00018000,
                mix_linear);
    }

    /* The two that answer the contention question, at the voice count that
     * matters. */
    run_one("linear", "striped", 1, samples_striped, 18, 0x00018000, mix_linear);
    run_one("linear", "scratch", 0, samples_scratch, 18, 0x00018000, mix_linear);
    run_one("linear", "scratch", 1, samples_scratch, 18, 0x00018000, mix_linear);

    spike_case_state("mix", "DONE");
}

int main(void) {
    spike_begin(SPIKE_NAME, SPIKE_VERSION);

    for (uint32_t i = 0; i < SAMPLE_FRAMES; i++) {
        int16_t v = (int16_t)((i * 517) & 0x7FFF) - 0x4000;
        samples_striped[i] = v;
        samples_scratch[i] = v;
    }

    spike_result("identity", "sys_clk_hz=%lu block_frames=%d blocks=%d",
                 (unsigned long)clock_get_hz(clk_sys), BLOCK_FRAMES, BLOCKS);

    case_mix();

    spike_report_previous();
    spike_done(SPIKE_NAME);

    while (true) {
        spike_pump();
        sleep_ms(10);
    }
}
