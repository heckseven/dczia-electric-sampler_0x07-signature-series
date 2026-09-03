/* SSD1306 128x32 over DMA'd I2C.
 *
 * Phase 0 measured this panel three ways and all three decided the design:
 *
 *   A full frame costs 12.78 ms of bus at 400 kHz but only 38 us of CPU - 0.3%
 *   of a core - once the transfer is DMA'd. The 32 ms in the project notes was
 *   `displayio`, and roughly 19 ms of it was software rather than wire.
 *
 *   A single page is 3.23 ms instead of 12.78. Since nothing on this screen
 *   changes all four pages at once, redrawing only what moved is both faster
 *   and a quarter of the supply disturbance.
 *
 *   That disturbance is the whole reason to care. The null test proved the
 *   display cannot perturb the digital audio path - the output words were
 *   bit-identical with the bus idle and with it hammering - so the pop the
 *   firmware works around is analog, and the only lever software has on it is
 *   how long the bus is busy.
 *
 * So: a framebuffer, per-page dirty tracking, and never a full-frame write
 * unless all four pages actually changed.
 */

#include <string.h>

#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "pico/stdlib.h"

#include "board.h"
#include "display.h"

#define PAGES (OLED_HEIGHT / 8)

static uint8_t frame[OLED_WIDTH * PAGES];

/* What the panel is actually showing. Dirtiness is decided by comparing against
 * this at flush time rather than by noting every write.
 *
 * The difference is not bookkeeping. Drawing a bar means clearing its strip and
 * redrawing it, which touches bytes twice and lands back where it started - so
 * write-marking reports a change on every frame, and the first version of this
 * file sent 2.8 pages every frame while the badge sat untouched. At 3.23 ms a
 * page and 30 frames a second that is 29% bus duty for no visible difference,
 * on the one bus whose activity is suspected of causing the pop. */
static uint8_t shadow[OLED_WIDTH * PAGES];

/* RP2040's I2C takes 16-bit writes into DATA_CMD so the STOP bit travels with
 * the byte, which is what lets a whole page go out under DMA rather than a byte
 * at a time. One page plus its control byte. */
static uint16_t commands[OLED_WIDTH + 2];
static int dma_i2c = -1;

static void burst(uint8_t control, const uint8_t *data, uint32_t length) {
    commands[0] = control;
    for (uint32_t i = 0; i < length; i++) {
        commands[i + 1] = data[i];
    }
    commands[length] |= I2C_IC_DATA_CMD_STOP_BITS;

    OLED_I2C->hw->enable = 0;
    OLED_I2C->hw->tar = OLED_ADDR;
    OLED_I2C->hw->enable = 1;

    dma_channel_config c = dma_channel_get_default_config(dma_i2c);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_16);
    channel_config_set_read_increment(&c, true);
    channel_config_set_write_increment(&c, false);
    channel_config_set_dreq(&c, i2c_get_dreq(OLED_I2C, true));
    dma_channel_configure(dma_i2c, &c, &OLED_I2C->hw->data_cmd, commands,
                          length + 1, true);
    dma_channel_wait_for_finish_blocking(dma_i2c);
    while ((OLED_I2C->hw->status & I2C_IC_STATUS_TFE_BITS) == 0) {
        tight_loop_contents();
    }
}

static void command(uint8_t c) {
    burst(0x00, &c, 1);
}

void display_init(void) {
    i2c_init(OLED_I2C, OLED_BAUDRATE);
    gpio_set_function(PIN_OLED_SDA, GPIO_FUNC_I2C);
    gpio_set_function(PIN_OLED_SCL, GPIO_FUNC_I2C);
    gpio_pull_up(PIN_OLED_SDA);
    gpio_pull_up(PIN_OLED_SCL);
    if (dma_i2c < 0) {
        dma_i2c = dma_claim_unused_channel(true);
    }

    static const uint8_t sequence[] = {
        0xAE,                    /* off while it is configured */
        0xD5, 0x80,              /* clock divide */
        0xA8, OLED_HEIGHT - 1,   /* multiplex ratio */
        0xD3, 0x00,              /* display offset */
        0x40,                    /* start line 0 */
        0x8D, 0x14,              /* charge pump on */
        0x20, 0x00,              /* horizontal addressing */
        0xA1, 0xC8,              /* segment remap, COM scan descending */
        0xDA, 0x02,              /* COM pins, the 128x32 layout */
        0x81, 0x8F,              /* contrast */
        0xD9, 0xF1, 0xDB, 0x40,  /* precharge, VCOM deselect */
        0xA4,                    /* follow RAM, not all-on */
        0xA6,                    /* normal, not inverted */
        0xAF,                    /* on */
    };
    for (uint32_t i = 0; i < count_of(sequence); i++) {
        command(sequence[i]);
    }

    memset(frame, 0, sizeof(frame));
    /* Anything but the frame, so the first flush writes every page. */
    memset(shadow, 0xFF, sizeof(shadow));
    display_flush();
}

void display_clear(void) {
    memset(frame, 0, sizeof(frame));
}

void display_pixel(uint32_t x, uint32_t y, bool on) {
    if (x >= OLED_WIDTH || y >= OLED_HEIGHT) {
        return;
    }
    uint8_t bit = (uint8_t)(1u << (y % 8));
    uint8_t *cell = &frame[(y / 8) * OLED_WIDTH + x];
    *cell = on ? (uint8_t)(*cell | bit) : (uint8_t)(*cell & ~bit);
}

void display_fill_rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h,
                       bool on) {
    for (uint32_t dy = 0; dy < h; dy++) {
        for (uint32_t dx = 0; dx < w; dx++) {
            display_pixel(x + dx, y + dy, on);
        }
    }
}

void display_rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, bool on) {
    if (w == 0 || h == 0) {
        return;
    }
    for (uint32_t dx = 0; dx < w; dx++) {
        display_pixel(x + dx, y, on);
        display_pixel(x + dx, y + h - 1, on);
    }
    for (uint32_t dy = 0; dy < h; dy++) {
        display_pixel(x, y + dy, on);
        display_pixel(x + w - 1, y + dy, on);
    }
}

uint32_t display_flush(void) {
    uint32_t written = 0;
    for (uint32_t page = 0; page < PAGES; page++) {
        uint8_t *row = &frame[page * OLED_WIDTH];
        uint8_t *shown = &shadow[page * OLED_WIDTH];
        if (memcmp(row, shown, OLED_WIDTH) == 0) {
            continue;
        }
        memcpy(shown, row, OLED_WIDTH);
        /* Window this page only. Three commands, then one DMA'd row. */
        command(0x21);
        command(0x00);
        command(OLED_WIDTH - 1);
        command(0x22);
        command((uint8_t)page);
        command((uint8_t)page);
        burst(0x40, row, OLED_WIDTH);
        written++;
    }
    return written;
}
