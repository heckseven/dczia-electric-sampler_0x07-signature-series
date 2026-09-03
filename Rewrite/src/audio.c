/* See audio.h for why this is shaped the way it is. */

#include <string.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/structs/systick.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "i2s.pio.h"

/* --- state ---------------------------------------------------------------- *
 *
 * Everything core 0 writes and core 1 reads is a single naturally-aligned word,
 * so a torn read is impossible on this architecture and no lock is needed. The
 * one exception is a sample pointer paired with its length, which cannot be
 * written atomically - `audio_set_sample` silences the track's voices first, so
 * the mixer is never looking at the pair while it changes.
 */

struct voice {
    const int16_t *data; /* NULL when the voice is free */
    uint32_t frames;
    uint32_t phase; /* 16.16 into data */
    uint32_t step;  /* 16.16 per output frame */
    int16_t gain;   /* Q15, copied from the track at trigger time */
    uint32_t age;   /* for choosing which voice a retrigger steals */
};

struct track {
    const int16_t *data;
    uint32_t frames;
    uint32_t step;
    int16_t gain;
};

static struct voice voices[VOICE_COUNT];
static struct track tracks[TRACK_COUNT];
static volatile int16_t master_gain = 0x4000; /* -6 dB, a safe first sound */

static int16_t arena[ARENA_BYTES / sizeof(int16_t)];
static uint32_t arena_used_frames;

/* Two blocks of stereo frames. Mono is duplicated into both channels: I2S is
 * framed in pairs, and the amp takes one of them. */
static uint32_t block_buffer[2][BLOCK_FRAMES];

static PIO audio_pio = pio0;
static uint sm_i2s;
static int dma_a = -1, dma_b = -1;

static volatile uint32_t stat_underruns;
static volatile uint32_t stat_blocks;
static volatile uint32_t stat_worst_cycles;
static volatile uint32_t stat_active;
static volatile uint32_t stat_peak_active;
/* Which tracks have a voice sounding. The display reads this every frame, so it
 * is a single word rather than something that needs walking. */
static volatile uint32_t active_mask;

/* Output checksum, for proving that something outside the audio path did not
 * change what the audio path emitted.
 *
 * Armed rather than started: the mixer restarts track 0's voice and resets the
 * sum in the same block, so two runs see byte-identical input and any
 * difference in the result is genuinely the thing under test. Starting it from
 * core 0 could not guarantee that - a block boundary could fall between the
 * trigger and the reset. */
static volatile uint32_t capture_arm;
static volatile uint32_t capture_remaining;
static volatile uint32_t capture_sum;
static volatile uint32_t trigger_age;

/* --- the mixer ------------------------------------------------------------ *
 *
 * The loop Phase 0 measured: a 16.16 phase accumulator, linear interpolation
 * between neighbouring samples, Q15 gain. 31.3 cycles per voice-frame, which at
 * 16 voices is about 6.4% of this core.
 *
 * Cortex-M0+ has no SMULL, no saturating arithmetic and no free operand shifts,
 * so every one of those is a separate instruction - which is why the estimate
 * that assumed otherwise was three times optimistic.
 */

static void __not_in_flash_func(mix_block)(uint32_t *out) {
    int32_t accumulator[BLOCK_FRAMES];
    memset(accumulator, 0, sizeof(accumulator));

    uint32_t arm = capture_arm;
    if (arm != 0) {
        for (uint32_t v = 0; v < VOICE_COUNT; v++) {
            voices[v].data = NULL;
        }
        voices[0].frames = tracks[0].frames;
        voices[0].phase = 0;
        voices[0].step = tracks[0].step;
        voices[0].gain = tracks[0].gain;
        voices[0].data = tracks[0].data;
        capture_sum = 0;
        capture_remaining = arm;
        capture_arm = 0;
    }

    int32_t master = master_gain;
    uint32_t active = 0;
    uint32_t mask = 0;

    for (uint32_t v = 0; v < VOICE_COUNT; v++) {
        const int16_t *data = voices[v].data;
        if (data == NULL) {
            continue;
        }
        active++;
        mask |= 1u << (v / VOICES_PER_TRACK);

        uint32_t phase = voices[v].phase;
        uint32_t step = voices[v].step;
        uint32_t frames = voices[v].frames;
        int32_t gain = voices[v].gain;

        /* The last frame has no neighbour to interpolate towards, so the loop
         * must stop one short of the end rather than read past it. */
        uint32_t limit = (frames - 1) << 16;

        for (uint32_t f = 0; f < BLOCK_FRAMES; f++) {
            if (phase >= limit) {
                /* Ran out mid-block. Free the voice and leave the rest of the
                 * block as it stands - the samples already summed are real. */
                data = NULL;
                break;
            }
            uint32_t index = phase >> 16;
            uint32_t frac = (phase >> 8) & 0xFF;
            int32_t a = data[index];
            int32_t b = data[index + 1];
            int32_t s = a + (((b - a) * (int32_t)frac) >> 8);
            accumulator[f] += (s * gain) >> 15;
            phase += step;
        }

        if (data == NULL) {
            voices[v].data = NULL;
        } else {
            voices[v].phase = phase;
        }
    }
    active_mask = mask;
    /* Peak, not instantaneous. A sample taken every two seconds almost always
     * lands between hits and reports zero, which says nothing about the load
     * the mixer actually carried. */
    stat_active = active;
    if (active > stat_peak_active) {
        stat_peak_active = active;
    }

    uint32_t sum = capture_sum;
    bool capturing = capture_remaining != 0;

    for (uint32_t f = 0; f < BLOCK_FRAMES; f++) {
        int32_t s = (accumulator[f] * master) >> 15;
        /* Clip rather than wrap. A wrapped sum is a full-scale discontinuity -
         * the loudest possible sound - where a clipped one is merely distorted,
         * and this drives a speaker somebody is holding. */
        if (s > 32767) {
            s = 32767;
        } else if (s < -32768) {
            s = -32768;
        }
        uint32_t word = (uint32_t)(uint16_t)(int16_t)s;
        out[f] = (word << 16) | word;
        if (capturing) {
            /* Order-sensitive, so a repeated or reordered block cannot cancel
             * out into a matching sum. */
            sum = (sum * 31u) + word;
        }
    }

    if (capturing) {
        capture_sum = sum;
        capture_remaining--;
    }
}

/* --- core 1 --------------------------------------------------------------- */

static void __not_in_flash_func(rewind)(int channel, uint32_t *buffer) {
    /* The plain registers, not their _trig aliases: the partner channel's chain
     * is what restarts this one, and triggering here as well would run two
     * transfers into the same FIFO. */
    dma_channel_hw_addr(channel)->read_addr = (uintptr_t)buffer;
    dma_channel_hw_addr(channel)->transfer_count = BLOCK_FRAMES;
}

static void __not_in_flash_func(serve)(int just_finished, uint32_t *buffer,
                                       int still_playing) {
    uint32_t start = systick_hw->cvr;

    mix_block(buffer);
    rewind(just_finished, buffer);

    /* If the buffer that was playing has also finished by now, the mix did not
     * beat the hardware and the DMA replayed stale audio. That is the only
     * definition of underrun available without listening. */
    if (!dma_channel_is_busy(still_playing)) {
        stat_underruns++;
    }

    /* SysTick counts down and is 24 bits. A block has 250,000 cycles in it, far
     * inside the 16.7 M the counter spans, so a single subtraction is right
     * unless it wrapped - and a mix that took 134 ms would have underrun
     * thousands of times over, which the counter above will have caught. */
    uint32_t elapsed = (start - systick_hw->cvr) & 0x00FFFFFFu;
    if (elapsed > stat_worst_cycles) {
        stat_worst_cycles = elapsed;
    }
    stat_blocks++;
}

static void __not_in_flash_func(core1_main)(void) {
    /* Core 1 has its own SysTick. Free-running, so `serve` can difference it. */
    systick_hw->rvr = 0x00FFFFFF;
    systick_hw->cvr = 0;
    systick_hw->csr = 0x5; /* enable, processor clock, no exception */

    for (;;) {
        while (dma_channel_is_busy(dma_a)) {
            tight_loop_contents();
        }
        serve(dma_a, block_buffer[0], dma_b);

        while (dma_channel_is_busy(dma_b)) {
            tight_loop_contents();
        }
        serve(dma_b, block_buffer[1], dma_a);
    }
}

/* --- setup ---------------------------------------------------------------- */

void audio_init(void) {
    audio_stop_all();
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        tracks[t].data = NULL;
        tracks[t].frames = 0;
        tracks[t].step = PITCH_UNITY;
        tracks[t].gain = 0x7FFF;
    }
    memset(block_buffer, 0, sizeof(block_buffer));

    uint offset = pio_add_program(audio_pio, &i2s_out_program);
    sm_i2s = pio_claim_unused_sm(audio_pio, true);
    /* 64 PIO cycles per stereo frame. 125 MHz / 1.024 MHz is 122.0703125, which
     * the 16.8 fixed-point divider holds exactly. */
    float div = (float)clock_get_hz(clk_sys) /
                (float)(SAMPLE_RATE * I2S_BITS_PER_FRAME);
    i2s_out_init(audio_pio, sm_i2s, offset, PIN_I2S_BCLK, PIN_I2S_DATA, div);

    dma_a = dma_claim_unused_channel(true);
    dma_b = dma_claim_unused_channel(true);

    dma_channel_config a = dma_channel_get_default_config(dma_a);
    channel_config_set_transfer_data_size(&a, DMA_SIZE_32);
    channel_config_set_read_increment(&a, true);
    channel_config_set_write_increment(&a, false);
    channel_config_set_dreq(&a, pio_get_dreq(audio_pio, sm_i2s, true));
    channel_config_set_chain_to(&a, dma_b);
    dma_channel_configure(dma_a, &a, &audio_pio->txf[sm_i2s], block_buffer[0],
                          BLOCK_FRAMES, false);

    dma_channel_config b = dma_channel_get_default_config(dma_b);
    channel_config_set_transfer_data_size(&b, DMA_SIZE_32);
    channel_config_set_read_increment(&b, true);
    channel_config_set_write_increment(&b, false);
    channel_config_set_dreq(&b, pio_get_dreq(audio_pio, sm_i2s, true));
    channel_config_set_chain_to(&b, dma_a);
    dma_channel_configure(dma_b, &b, &audio_pio->txf[sm_i2s], block_buffer[1],
                          BLOCK_FRAMES, false);
}

void audio_start(void) {
    pio_sm_set_enabled(audio_pio, sm_i2s, true);
    dma_channel_start(dma_a);
    multicore_launch_core1(core1_main);
}

/* --- arena ---------------------------------------------------------------- */

int16_t *audio_arena_alloc(uint32_t frames) {
    /* One spare frame per sample, so the interpolator's read of data[index + 1]
     * on the final frame lands inside the arena rather than one short of the
     * next sample's first frame. */
    uint32_t want = frames + 1;
    if (want > (ARENA_BYTES / sizeof(int16_t)) - arena_used_frames) {
        return NULL;
    }
    int16_t *at = &arena[arena_used_frames];
    arena_used_frames += want;
    at[frames] = 0;
    return at;
}

void audio_arena_reset(void) {
    /* Silence first. A voice still pointing into the arena would play whatever
     * is loaded over the top of it. */
    audio_stop_all();
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        tracks[t].data = NULL;
        tracks[t].frames = 0;
    }
    arena_used_frames = 0;
}

uint32_t audio_arena_used(void) {
    return arena_used_frames * sizeof(int16_t);
}

uint32_t audio_arena_free(void) {
    return ARENA_BYTES - audio_arena_used();
}

/* --- tracks --------------------------------------------------------------- */

static void silence_track(uint8_t track) {
    for (uint32_t v = 0; v < VOICES_PER_TRACK; v++) {
        voices[track * VOICES_PER_TRACK + v].data = NULL;
    }
}

void audio_set_sample(uint8_t track, const int16_t *data, uint32_t frames) {
    if (track >= TRACK_COUNT) {
        return;
    }
    /* Silence before the pair changes: pointer and length cannot be written
     * atomically together, and a voice reading a new pointer with an old length
     * would run off the end of the sample. */
    silence_track(track);
    tracks[track].data = (frames >= 2) ? data : NULL;
    tracks[track].frames = frames;
}

void audio_set_pitch(uint8_t track, uint32_t step) {
    if (track >= TRACK_COUNT || step == 0) {
        return;
    }
    tracks[track].step = step;
    /* Sounding voices follow the knob. A pitch change that only took effect on
     * the next trigger would feel broken while a pad is held. */
    for (uint32_t v = 0; v < VOICES_PER_TRACK; v++) {
        voices[track * VOICES_PER_TRACK + v].step = step;
    }
}

void audio_set_gain(uint8_t track, int16_t gain) {
    if (track < TRACK_COUNT) {
        tracks[track].gain = gain;
    }
}

void audio_trigger(uint8_t track) {
    if (track >= TRACK_COUNT) {
        return;
    }
    const int16_t *data = tracks[track].data;
    uint32_t frames = tracks[track].frames;
    if (data == NULL || frames < 2) {
        return;
    }

    uint32_t base = track * VOICES_PER_TRACK;
    uint32_t chosen = base;
    uint32_t oldest = 0xFFFFFFFFu;
    for (uint32_t v = 0; v < VOICES_PER_TRACK; v++) {
        if (voices[base + v].data == NULL) {
            chosen = base + v;
            oldest = 0;
            break;
        }
        if (voices[base + v].age < oldest) {
            oldest = voices[base + v].age;
            chosen = base + v;
        }
    }

    /* Order matters: clear the voice before pointing it at anything, so the
     * mixer cannot see a half-built voice between these writes. */
    voices[chosen].data = NULL;
    voices[chosen].phase = 0;
    voices[chosen].frames = frames;
    voices[chosen].step = tracks[track].step;
    voices[chosen].gain = tracks[track].gain;
    voices[chosen].age = ++trigger_age;
    voices[chosen].data = data;
}

void audio_stop(uint8_t track) {
    if (track < TRACK_COUNT) {
        silence_track(track);
    }
}

void audio_stop_all(void) {
    for (uint32_t v = 0; v < VOICE_COUNT; v++) {
        voices[v].data = NULL;
    }
}

void audio_set_master(int16_t gain) {
    master_gain = gain;
}

uint32_t audio_underruns(void) {
    return stat_underruns;
}

uint32_t audio_blocks(void) {
    return stat_blocks;
}

uint32_t audio_worst_cycles(void) {
    return stat_worst_cycles;
}

uint32_t audio_active_voices(void) {
    return stat_active;
}

uint32_t audio_peak_voices(void) {
    return stat_peak_active;
}

uint32_t audio_active_mask(void) {
    return active_mask;
}

void audio_capture_arm(uint32_t blocks) {
    capture_remaining = 0;
    capture_arm = blocks;
}

bool audio_capture_done(void) {
    return capture_arm == 0 && capture_remaining == 0;
}

uint32_t audio_capture_sum(void) {
    return capture_sum;
}
