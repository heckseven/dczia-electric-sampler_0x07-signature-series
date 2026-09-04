/* The ten NeoPixels, driven by PIO so they cost no CPU.
 *
 * A WS2812 wants 24 bits a pixel at 800 kHz with sub-microsecond timing, which
 * is exactly what a bit-banged driver cannot promise on a chip that is also
 * mixing audio - the CircuitPython version disables interrupts for the length
 * of the transfer, 300 us for ten pixels, every time it shows a frame. Here the
 * PIO holds the timing and a DMA channel feeds it, so pushing a frame costs one
 * register write and the audio never knows it happened.
 *
 * Geometry is measured, not derived. utils.neoindex records what was actually
 * under each pixel when it was lit one at a time; two earlier tables derived
 * from the board layout were both wrong, because the LEDs are on the back
 * copper of the front panel and the switches on the front copper of the main
 * board, and nothing records how the two are mounted relative to each other.
 */

#ifndef PIXELS_H
#define PIXELS_H

#include <stdbool.h>
#include <stdint.h>

#include "board.h"

/* Pad 1-8 to pixel index, then the two buttons. From utils.neoindex:
 *
 *     pixel 0    Function
 *     pixel 1    Play
 *     pixel 2-5  upper pad row, RIGHT to left  (pads 4, 3, 2, 1)
 *     pixel 6-9  lower pad row, left to right  (pads 5, 6, 7, 8)
 */
#define PIXEL_PLAY 1
#define PIXEL_FUNCTION 0
extern const uint8_t PIXEL_FOR_PAD[8];

void pixels_init(void);

/* Set one pixel. Nothing reaches the strip until pixels_show. */
void pixels_set(uint32_t index, uint8_t r, uint8_t g, uint8_t b);

/* Push the frame, if it differs from the one already showing.
 *
 * Skipping an unchanged frame is most of them: at a slow tempo the strip holds
 * still between sixteenths, and the panel is redrawn far more often than it
 * changes. Returns true if anything was actually sent. */
bool pixels_show(void);

/* Global brightness, 0-255. The Python runs the strip at 0.1, which is what
 * makes ten pixels an inch from the eye readable rather than blinding. */
void pixels_set_brightness(uint8_t level);

uint32_t pixels_frames_sent(void);

#endif /* PIXELS_H */
