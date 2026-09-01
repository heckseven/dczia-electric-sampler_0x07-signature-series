/* Task 6: what the SD card actually delivers, and how badly it stalls.
 *
 * This is the measurement behind "can samples be longer than RAM". Holding a
 * kit in RAM caps a sample at whatever fits; streaming caps it at the card. The
 * current firmware tried streaming and it failed - but it failed on FatFS
 * reentrancy and GC pauses, not on the card, so the card's own numbers were
 * never taken.
 *
 * Two different questions, and they want different answers:
 *
 *   (a) Streaming. Per-block read time, p50/p99/max, at three clocks. The
 *       hazard is not the typical read - it is a card garbage-collection stall
 *       of hundreds of milliseconds, arriving rarely. A clean 60 s run does NOT
 *       bound that tail, and this file says so rather than implying otherwise.
 *
 *   (b) Loading a kit. Sequential throughput for 32/64/128 KB, which is a
 *       boot-time cost rather than a real-time one, and the number the
 *       RAM-resident design actually needs.
 *
 * Plus two workarounds the Python carries, to see whether a C driver still
 * needs them: the deliberately discarded first read (commit "Spend an SD card's
 * first read, which some cards fail") and the 22-bit C_SIZE of CSD v2.
 *
 * No filesystem. FatFS is what broke last time, and a raw block driver measures
 * the card rather than the layer above it.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/spi.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"

#include "spike_common.h"

#define SPIKE_NAME "sd"
#define SPIKE_VERSION 1

/* setup.py: busio.SPI(board.GP10, board.GP11, board.GP12) then CS on GP13. */
#define PIN_SCK 10
#define PIN_MOSI 11
#define PIN_MISO 12
#define PIN_CS 13

#define SD_SPI spi1
#define BLOCK 512

/* 60 s per clock, as the plan asks. Long enough to see a typical distribution,
 * and explicitly NOT long enough to bound a rare garbage-collection stall. */
#define SOAK_US (60u * 1000u * 1000u)

static uint8_t buffer[BLOCK];
static uint8_t dummy_tx[BLOCK];
static int dma_rx = -1, dma_tx = -1;
static bool block_addressed;

/* --- histogram ------------------------------------------------------------ *
 *
 * Streamed into fixed buckets rather than kept as samples: a 60 s soak at a few
 * hundred microseconds a read is hundreds of thousands of reads, and keeping
 * them would need more RAM than the chip has. Learned the hard way in the
 * CircuitPython baseline spike, which died with MemoryError trying.
 */

#define FINE_US 10
#define FINE_BUCKETS 200 /* 0 .. 2000 us */
static const uint32_t COARSE_EDGES_US[] = {2000,  5000,   10000,  25000,
                                           50000, 100000, 250000, 500000};
#define COARSE_BUCKETS (count_of(COARSE_EDGES_US) + 1)

struct hist {
    uint32_t fine[FINE_BUCKETS];
    uint32_t coarse[COARSE_BUCKETS];
    uint32_t count;
    uint32_t min;
    uint32_t max;
    uint64_t total;
};

static struct hist soak;

static void hist_reset(struct hist *h) {
    memset(h, 0, sizeof(*h));
    h->min = 0xFFFFFFFFu;
}

static void hist_add(struct hist *h, uint32_t us) {
    h->count++;
    h->total += us;
    if (us < h->min) {
        h->min = us;
    }
    if (us > h->max) {
        h->max = us;
    }
    if (us < FINE_US * FINE_BUCKETS) {
        h->fine[us / FINE_US]++;
        return;
    }
    for (uint32_t i = 0; i < count_of(COARSE_EDGES_US); i++) {
        if (us < COARSE_EDGES_US[i]) {
            h->coarse[i]++;
            return;
        }
    }
    h->coarse[COARSE_BUCKETS - 1]++;
}

/* Upper edge of the bucket holding the given percentile. Reported as an upper
 * bound because that is what a bucketed histogram actually knows. */
static uint32_t hist_percentile(const struct hist *h, uint32_t permille) {
    uint32_t want = (uint32_t)(((uint64_t)h->count * permille) / 1000u);
    uint32_t seen = 0;
    for (uint32_t i = 0; i < FINE_BUCKETS; i++) {
        seen += h->fine[i];
        if (seen >= want) {
            return (i + 1) * FINE_US;
        }
    }
    for (uint32_t i = 0; i < count_of(COARSE_EDGES_US); i++) {
        seen += h->coarse[i];
        if (seen >= want) {
            return COARSE_EDGES_US[i];
        }
    }
    return h->max;
}

/* --- raw SPI -------------------------------------------------------------- */

static inline uint8_t xfer(uint8_t v) {
    uint8_t r = 0xFF;
    spi_write_read_blocking(SD_SPI, &v, &r, 1);
    return r;
}

static inline void cs(bool low) {
    gpio_put(PIN_CS, !low);
}

static uint8_t command(uint8_t index, uint32_t arg, uint8_t crc) {
    /* A card can hold MISO low from the previous operation; a byte of clock
     * with CS asserted lets it finish. */
    xfer(0xFF);
    xfer(0x40 | index);
    xfer(arg >> 24);
    xfer(arg >> 16);
    xfer(arg >> 8);
    xfer(arg);
    xfer(crc);
    /* R1 arrives within 8 bytes; the top bit is clear when it does. */
    for (int i = 0; i < 10; i++) {
        uint8_t r = xfer(0xFF);
        if (!(r & 0x80)) {
            return r;
        }
    }
    return 0xFF;
}

static bool wait_token(uint8_t want, uint32_t timeout_us) {
    uint32_t deadline = timer_hw->timerawl + timeout_us;
    while ((int32_t)(timer_hw->timerawl - deadline) < 0) {
        if (xfer(0xFF) == want) {
            return true;
        }
    }
    return false;
}

/* --- card bring-up -------------------------------------------------------- */

static bool sd_init(uint32_t *ocr_out, uint8_t *csd_out) {
    spi_set_baudrate(SD_SPI, 400000);
    cs(false);
    /* At least 74 clocks with CS high, per the spec's power-up sequence. */
    for (int i = 0; i < 10; i++) {
        xfer(0xFF);
    }

    cs(true);
    if (command(0, 0, 0x95) != 0x01) {
        cs(false);
        return false;
    }

    /* CMD8 separates v2 from v1. 0x1AA is 2.7-3.6 V plus a check pattern the
     * card echoes back. */
    uint8_t r = command(8, 0x1AA, 0x87);
    bool v2 = (r == 0x01);
    if (v2) {
        for (int i = 0; i < 4; i++) {
            xfer(0xFF);
        }
    }

    /* ACMD41 with the host-capacity-support bit, until the card leaves idle. */
    uint32_t deadline = timer_hw->timerawl + 2000000u;
    bool ready = false;
    while ((int32_t)(timer_hw->timerawl - deadline) < 0) {
        command(55, 0, 0x65);
        if (command(41, v2 ? 0x40000000u : 0, 0x77) == 0x00) {
            ready = true;
            break;
        }
    }
    if (!ready) {
        cs(false);
        return false;
    }

    /* CMD58 reads the OCR; bit 30 (CCS) says addresses are blocks, not bytes.
     * Getting this wrong on a 29 GB card reads the wrong place rather than
     * failing, which is why it is reported rather than assumed. */
    uint32_t ocr = 0;
    if (command(58, 0, 0xFD) == 0x00) {
        for (int i = 0; i < 4; i++) {
            ocr = (ocr << 8) | xfer(0xFF);
        }
    }
    block_addressed = (ocr & 0x40000000u) != 0;
    *ocr_out = ocr;

    if (!block_addressed) {
        command(16, BLOCK, 0xFF); /* byte-addressed cards need an explicit len */
    }

    /* CMD9 for the CSD, so the 22-bit C_SIZE question can be answered from the
     * card rather than from the Python's comment about it. */
    memset(csd_out, 0, 16);
    if (command(9, 0, 0xFF) == 0x00 && wait_token(0xFE, 200000)) {
        for (int i = 0; i < 16; i++) {
            csd_out[i] = xfer(0xFF);
        }
        xfer(0xFF);
        xfer(0xFF);
    }

    cs(false);
    return true;
}

/* --- reads ---------------------------------------------------------------- */

static bool read_block(uint32_t lba) {
    cs(true);
    uint32_t arg = block_addressed ? lba : lba * BLOCK;
    if (command(17, arg, 0xFF) != 0x00) {
        cs(false);
        return false;
    }
    if (!wait_token(0xFE, 500000)) {
        cs(false);
        return false;
    }

    /* DMA the payload. Polling 512 bytes a byte at a time is measuring the
     * driver, not the card. */
    dma_channel_config rc = dma_channel_get_default_config(dma_rx);
    channel_config_set_transfer_data_size(&rc, DMA_SIZE_8);
    channel_config_set_read_increment(&rc, false);
    channel_config_set_write_increment(&rc, true);
    channel_config_set_dreq(&rc, spi_get_dreq(SD_SPI, false));
    dma_channel_configure(dma_rx, &rc, buffer, &spi_get_hw(SD_SPI)->dr, BLOCK,
                          false);

    dma_channel_config tc = dma_channel_get_default_config(dma_tx);
    channel_config_set_transfer_data_size(&tc, DMA_SIZE_8);
    channel_config_set_read_increment(&tc, false);
    channel_config_set_write_increment(&tc, false);
    channel_config_set_dreq(&tc, spi_get_dreq(SD_SPI, true));
    dma_channel_configure(dma_tx, &tc, &spi_get_hw(SD_SPI)->dr, dummy_tx, BLOCK,
                          false);

    dma_start_channel_mask((1u << dma_rx) | (1u << dma_tx));
    dma_channel_wait_for_finish_blocking(dma_rx);
    dma_channel_wait_for_finish_blocking(dma_tx);

    xfer(0xFF); /* CRC, discarded - SPI mode does not check it by default */
    xfer(0xFF);
    cs(false);
    return true;
}

/* --- cases ---------------------------------------------------------------- */

static void report(const char *label, uint32_t hz, const struct hist *h,
                   uint32_t failures) {
    uint32_t mean = h->count ? (uint32_t)(h->total / h->count) : 0;
    /* 512 bytes per read, expressed as KB/s: 512 * 1e6 / us / 1024. */
    uint32_t kbps = mean ? (uint32_t)((512ull * 1000000ull) / mean / 1024ull) : 0;
    spike_result("soak",
                 "phase=%s hz=%lu reads=%lu fail=%lu min_us=%lu mean_us=%lu "
                 "p50_us=%lu p99_us=%lu p999_us=%lu max_us=%lu mean_kbps=%lu",
                 label, (unsigned long)hz, (unsigned long)h->count,
                 (unsigned long)failures,
                 (unsigned long)(h->count ? h->min : 0),
                 (unsigned long)mean,
                 (unsigned long)hist_percentile(h, 500),
                 (unsigned long)hist_percentile(h, 990),
                 (unsigned long)hist_percentile(h, 999),
                 (unsigned long)h->max, (unsigned long)kbps);
}

static void case_soak(void) {
    spike_case("soak");

    static const uint32_t clocks[] = {24000000, 16000000, 8000000};
    for (uint32_t ci = 0; ci < count_of(clocks); ci++) {
        uint32_t want = clocks[ci];
        uint32_t got = spi_set_baudrate(SD_SPI, want);

        hist_reset(&soak);
        uint32_t failures = 0;
        uint32_t lba = 1024; /* clear of any boot sector */
        uint32_t deadline = timer_hw->timerawl + SOAK_US;

        while ((int32_t)(timer_hw->timerawl - deadline) < 0) {
            uint32_t t0 = timer_hw->timerawl;
            bool ok = read_block(lba);
            uint32_t us = timer_hw->timerawl - t0;
            if (ok) {
                hist_add(&soak, us);
            } else {
                failures++;
            }
            /* Sequential, wrapping inside 64 MB: a stream reads forwards, and a
             * card behaves differently for that than for one block hammered. */
            lba++;
            if (lba >= 1024 + 131072) {
                lba = 1024;
            }
            if ((soak.count & 0x3FF) == 0) {
                spike_pump();
            }
        }
        report("soak", got, &soak, failures);
    }

    spike_case_state("soak", "DONE");
}

static void case_kit(void) {
    spike_case("kit");

    static const uint32_t sizes_kb[] = {32, 64, 128};
    uint32_t got = spi_set_baudrate(SD_SPI, 24000000);

    for (uint32_t si = 0; si < count_of(sizes_kb); si++) {
        uint32_t blocks = (sizes_kb[si] * 1024u) / BLOCK;
        uint32_t t0 = timer_hw->timerawl;
        uint32_t failures = 0;
        for (uint32_t i = 0; i < blocks; i++) {
            if (!read_block(1024 + i)) {
                failures++;
            }
        }
        uint32_t us = timer_hw->timerawl - t0;
        uint32_t kbps = us ? (uint32_t)(((uint64_t)sizes_kb[si] * 1000000ull) / us)
                           : 0;
        spike_result("kit", "hz=%lu kb=%lu us=%lu fail=%lu kbps=%lu",
                     (unsigned long)got, (unsigned long)sizes_kb[si],
                     (unsigned long)us, (unsigned long)failures,
                     (unsigned long)kbps);
        spike_pump();
    }

    spike_case_state("kit", "DONE");
}

/* Does a C driver still need the two workarounds the Python carries? */
static void case_workarounds(uint32_t ocr, const uint8_t *csd) {
    spike_case("workarounds");

    /* (1) The discarded first read. Re-initialise and check whether the very
     * first block after bring-up differs from the same block read again. */
    uint32_t first_sum = 0, second_sum = 0;
    bool first_ok = read_block(2048);
    for (uint32_t i = 0; i < BLOCK; i++) {
        first_sum += buffer[i];
    }
    bool second_ok = read_block(2048);
    for (uint32_t i = 0; i < BLOCK; i++) {
        second_sum += buffer[i];
    }
    spike_result("workarounds",
                 "first_read_ok=%d second_read_ok=%d first_sum=%lu "
                 "second_sum=%lu first_read_needed=%d",
                 first_ok ? 1 : 0, second_ok ? 1 : 0,
                 (unsigned long)first_sum, (unsigned long)second_sum,
                 (first_ok && second_ok && first_sum == second_sum) ? 0 : 1);

    /* Raw CSD, so a capacity that disagrees with another driver's can be
     * settled from the bytes rather than by picking a side. */
    spike_result("workarounds",
                 "csd=%02x%02x%02x%02x%02x%02x%02x%02x"
                 "%02x%02x%02x%02x%02x%02x%02x%02x",
                 csd[0], csd[1], csd[2], csd[3], csd[4], csd[5], csd[6],
                 csd[7], csd[8], csd[9], csd[10], csd[11], csd[12], csd[13],
                 csd[14], csd[15]);

    /* (2) CSD version and the 22-bit C_SIZE. CSD v2 puts capacity in a 22-bit
     * field at bits 69:48, and capacity is (C_SIZE + 1) * 512 KB. */
    uint8_t version = csd[0] >> 6;
    uint32_t c_size = 0;
    uint32_t blocks = 0;
    if (version == 1) {
        c_size = ((uint32_t)(csd[7] & 0x3F) << 16) | ((uint32_t)csd[8] << 8) |
                 csd[9];
        blocks = (c_size + 1) * 1024u;
    }
    spike_result("workarounds",
                 "csd_version=%d c_size=%lu blocks=%lu bytes_mb=%lu ocr=0x%08lx "
                 "block_addressed=%d",
                 version + 1, (unsigned long)c_size, (unsigned long)blocks,
                 (unsigned long)((uint64_t)blocks * BLOCK / (1024 * 1024)),
                 (unsigned long)ocr, block_addressed ? 1 : 0);

    /* (3) Why CircuitPython cannot mount this card.
     *
     * Not in the plan, and worth the twenty lines: the badge boots from flash
     * samples because storage.mount() fails, and setup.py already retries every
     * baudrate, so the card is answering and the filesystem is not. The MBR
     * partition type says which - CircuitPython's VfsFat does FAT12/16/32 and
     * nothing else, and a card shipped as exFAT would look exactly like this.
     */
    if (read_block(0)) {
        bool signature = buffer[510] == 0x55 && buffer[511] == 0xAA;
        for (uint32_t i = 0; i < 4; i++) {
            const uint8_t *e = &buffer[446 + i * 16];
            uint32_t start = (uint32_t)e[8] | ((uint32_t)e[9] << 8) |
                             ((uint32_t)e[10] << 16) | ((uint32_t)e[11] << 24);
            uint32_t count = (uint32_t)e[12] | ((uint32_t)e[13] << 8) |
                             ((uint32_t)e[14] << 16) | ((uint32_t)e[15] << 24);
            if (e[4] == 0 && count == 0) {
                continue;
            }
            spike_result("workarounds",
                         "mbr_sig=%d part=%lu type=0x%02x lba=%lu blocks=%lu "
                         "mb=%lu",
                         signature ? 1 : 0, (unsigned long)i,
                         e[4], (unsigned long)start, (unsigned long)count,
                         (unsigned long)((uint64_t)count * BLOCK / (1024 * 1024)));
        }
        if (!signature) {
            spike_result("workarounds", "mbr_sig=0 no partition table at LBA 0");
        }
    }

    spike_case_state("workarounds", "DONE");
}

int main(void) {
    spike_begin(SPIKE_NAME, SPIKE_VERSION);

    memset(dummy_tx, 0xFF, sizeof(dummy_tx));

    spi_init(SD_SPI, 400000);
    gpio_set_function(PIN_SCK, GPIO_FUNC_SPI);
    gpio_set_function(PIN_MOSI, GPIO_FUNC_SPI);
    gpio_set_function(PIN_MISO, GPIO_FUNC_SPI);
    gpio_init(PIN_CS);
    gpio_set_dir(PIN_CS, GPIO_OUT);
    gpio_put(PIN_CS, 1);

    dma_rx = dma_claim_unused_channel(true);
    dma_tx = dma_claim_unused_channel(true);

    uint32_t ocr = 0;
    uint8_t csd[16];
    bool ok = sd_init(&ocr, csd);
    spike_result("identity", "sys_clk_hz=%lu card_init=%d peri_hz=%lu",
                 (unsigned long)clock_get_hz(clk_sys), ok ? 1 : 0,
                 (unsigned long)clock_get_hz(clk_peri));

    if (!ok) {
        spike_result("identity", "NO CARD - nothing further can be measured");
        spike_done(SPIKE_NAME);
        while (true) {
            spike_pump();
            sleep_ms(10);
        }
    }

    case_workarounds(ocr, csd);
    case_kit();
    case_soak();

    spike_report_previous();
    spike_done(SPIKE_NAME);

    while (true) {
        spike_pump();
        sleep_ms(10);
    }
}
