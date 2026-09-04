/* See pixels.h. */

#include <string.h>

#include "hardware/dma.h"
#include "hardware/pio.h"
#include "pico/stdlib.h"

#include "pixels.h"
#include "ws2812.pio.h"

/* pio1, because pio0 is the I2S output. Four state machines each and the audio
 * needs one of pio0's; putting the strip on the other block means a future
 * program on either cannot crowd out the one that must never stall. */
static PIO pixel_pio = pio1;
static uint sm;
static int dma_chan = -1;

const uint8_t PIXEL_FOR_PAD[8] = {5, 4, 3, 2, 6, 7, 8, 9};

/* One word a pixel, GRB in the top 24 bits - the order the part expects and the
 * order the PIO program shifts out, MSB first. */
static uint32_t frame[NEOPIXEL_COUNT];
static uint32_t shown[NEOPIXEL_COUNT];
static uint8_t raw[NEOPIXEL_COUNT][3];
static uint8_t brightness = 26; /* 0.1 of full, matching the Python */
static uint32_t frames_sent;
static bool ever_shown;

void pixels_set(uint32_t index, uint8_t r, uint8_t g, uint8_t b) {
    if (index >= NEOPIXEL_COUNT) {
        return;
    }
    raw[index][0] = r;
    raw[index][1] = g;
    raw[index][2] = b;
}

void pixels_set_brightness(uint8_t level) {
    brightness = level;
}

bool pixels_show(void) {
    for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
        /* Scaled here rather than at pixels_set, so changing the brightness
         * does not need every colour set again - and so the colours the
         * animations produce stay the colours view.py names, at full range,
         * however dim the strip is being run. */
        uint32_t r = ((uint32_t)raw[i][0] * brightness) / 255u;
        uint32_t g = ((uint32_t)raw[i][1] * brightness) / 255u;
        uint32_t b = ((uint32_t)raw[i][2] * brightness) / 255u;
        frame[i] = (g << 24) | (r << 16) | (b << 8);
    }

    if (ever_shown && memcmp(frame, shown, sizeof(frame)) == 0) {
        return false;
    }

    /* Still sending the last one. Dropping this frame is right: the next pass
     * will send whatever is current, which is fresher than what would be
     * queued here, and a strip is not a thing anyone can see one frame of. */
    if (dma_chan >= 0 && dma_channel_is_busy(dma_chan)) {
        return false;
    }

    memcpy(shown, frame, sizeof(frame));
    ever_shown = true;
    dma_channel_set_read_addr(dma_chan, shown, false);
    dma_channel_set_trans_count(dma_chan, NEOPIXEL_COUNT, true);
    frames_sent++;
    return true;
}

uint32_t pixels_frames_sent(void) {
    return frames_sent;
}

void pixels_init(void) {
    uint offset = pio_add_program(pixel_pio, &ws2812_program);
    sm = pio_claim_unused_sm(pixel_pio, true);
    ws2812_init(pixel_pio, sm, offset, PIN_NEOPIXEL);

    dma_chan = dma_claim_unused_channel(true);
    dma_channel_config c = dma_channel_get_default_config(dma_chan);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_32);
    channel_config_set_read_increment(&c, true);
    channel_config_set_write_increment(&c, false);
    channel_config_set_dreq(&c, pio_get_dreq(pixel_pio, sm, true));
    dma_channel_configure(dma_chan, &c, &pixel_pio->txf[sm], shown,
                          NEOPIXEL_COUNT, false);

    /* Start dark. A strip left holding whatever was in its registers at reset
     * is ten bright pixels an inch from the player's eye. */
    memset(raw, 0, sizeof(raw));
    pixels_show();
}
