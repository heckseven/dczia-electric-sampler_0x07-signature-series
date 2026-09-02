/* See sd.h. Lifted from Spikes/c/spike_sd.c, which measured this card over
 * 615,000 reads without a failure - so this is the sequence that was proven,
 * not a fresh attempt at the same thing. */

#include <string.h>

#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/spi.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"

#include "board.h"
#include "sd.h"

static bool block_addressed;
static uint32_t capacity_blocks;
static int dma_rx = -1, dma_tx = -1;
static uint8_t dummy_tx[SD_BLOCK];

static inline uint8_t xfer(uint8_t v) {
    uint8_t r = 0xFF;
    spi_write_read_blocking(SD_SPI, &v, &r, 1);
    return r;
}

static inline void cs(bool low) {
    gpio_put(PIN_SD_CS, !low);
}

static uint8_t command(uint8_t index, uint32_t arg, uint8_t crc) {
    /* A card can still be holding MISO from the previous operation; one byte of
     * clock with CS asserted lets it finish. */
    xfer(0xFF);
    xfer(0x40 | index);
    xfer(arg >> 24);
    xfer(arg >> 16);
    xfer(arg >> 8);
    xfer(arg);
    xfer(crc);
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

bool sd_read(uint32_t lba, uint8_t *buffer) {
    cs(true);
    uint32_t arg = block_addressed ? lba : lba * SD_BLOCK;
    if (command(17, arg, 0xFF) != 0x00 || !wait_token(0xFE, 500000)) {
        cs(false);
        return false;
    }

    /* DMA the payload. Polling 512 bytes one at a time measures the driver
     * rather than the card, and this is the path a kit load runs 64 times. */
    dma_channel_config rc = dma_channel_get_default_config(dma_rx);
    channel_config_set_transfer_data_size(&rc, DMA_SIZE_8);
    channel_config_set_read_increment(&rc, false);
    channel_config_set_write_increment(&rc, true);
    channel_config_set_dreq(&rc, spi_get_dreq(SD_SPI, false));
    dma_channel_configure(dma_rx, &rc, buffer, &spi_get_hw(SD_SPI)->dr, SD_BLOCK,
                          false);

    dma_channel_config tc = dma_channel_get_default_config(dma_tx);
    channel_config_set_transfer_data_size(&tc, DMA_SIZE_8);
    channel_config_set_read_increment(&tc, false);
    channel_config_set_write_increment(&tc, false);
    channel_config_set_dreq(&tc, spi_get_dreq(SD_SPI, true));
    dma_channel_configure(dma_tx, &tc, &spi_get_hw(SD_SPI)->dr, dummy_tx,
                          SD_BLOCK, false);

    dma_start_channel_mask((1u << dma_rx) | (1u << dma_tx));
    dma_channel_wait_for_finish_blocking(dma_rx);
    dma_channel_wait_for_finish_blocking(dma_tx);

    xfer(0xFF); /* CRC, discarded: SPI mode does not check it by default */
    xfer(0xFF);
    cs(false);
    return true;
}

bool sd_init(void) {
    memset(dummy_tx, 0xFF, sizeof(dummy_tx));

    spi_init(SD_SPI, 400000);
    gpio_set_function(PIN_SD_SCK, GPIO_FUNC_SPI);
    gpio_set_function(PIN_SD_MOSI, GPIO_FUNC_SPI);
    gpio_set_function(PIN_SD_MISO, GPIO_FUNC_SPI);
    gpio_init(PIN_SD_CS);
    gpio_set_dir(PIN_SD_CS, GPIO_OUT);
    gpio_put(PIN_SD_CS, 1);

    if (dma_rx < 0) {
        dma_rx = dma_claim_unused_channel(true);
        dma_tx = dma_claim_unused_channel(true);
    }

    cs(false);
    /* At least 74 clocks with CS high, per the power-up sequence. */
    for (int i = 0; i < 10; i++) {
        xfer(0xFF);
    }

    cs(true);
    if (command(0, 0, 0x95) != 0x01) {
        cs(false);
        return false;
    }

    /* CMD8 separates v2 from v1: 0x1AA is 2.7-3.6 V plus a pattern the card
     * echoes back. */
    uint8_t r = command(8, 0x1AA, 0x87);
    bool v2 = (r == 0x01);
    if (v2) {
        for (int i = 0; i < 4; i++) {
            xfer(0xFF);
        }
    }

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

    /* CCS in the OCR says whether addresses are blocks or bytes. Getting it
     * wrong on a 64 GB card reads the wrong place rather than failing. */
    uint32_t ocr = 0;
    if (command(58, 0, 0xFD) == 0x00) {
        for (int i = 0; i < 4; i++) {
            ocr = (ocr << 8) | xfer(0xFF);
        }
    }
    block_addressed = (ocr & 0x40000000u) != 0;
    if (!block_addressed) {
        command(16, SD_BLOCK, 0xFF);
    }

    uint8_t csd[16];
    memset(csd, 0, sizeof(csd));
    capacity_blocks = 0;
    if (command(9, 0, 0xFF) == 0x00 && wait_token(0xFE, 200000)) {
        for (int i = 0; i < 16; i++) {
            csd[i] = xfer(0xFF);
        }
        xfer(0xFF);
        xfer(0xFF);
        if ((csd[0] >> 6) == 1) {
            /* CSD v2: C_SIZE is the 22 bits at 69:48. All twenty-two of them. */
            uint32_t c_size = ((uint32_t)(csd[7] & 0x3F) << 16) |
                              ((uint32_t)csd[8] << 8) | csd[9];
            capacity_blocks = (c_size + 1) * 1024u;
        }
    }

    cs(false);
    spi_set_baudrate(SD_SPI, SD_BAUDRATE);

    /* Spend one read and ignore the result. Some cards fail the first one after
     * bring-up; CircuitPython's sdcardio fails it on this card at every
     * baudrate tried. The C driver did not, but the cost is a single block and
     * the alternative is a mount that fails for a reason nobody can see. */
    uint8_t scratch[SD_BLOCK];
    (void)sd_read(0, scratch);

    return true;
}

uint32_t sd_blocks(void) {
    return capacity_blocks;
}
