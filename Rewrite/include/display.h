/* The SSD1306, drawn as graphics rather than text.
 *
 * 128x32 is not much room, and while playing, what a player wants to see is
 * which pads are firing and where the two knobs are - not words. Blocks and
 * bars say that at a glance and, usefully, need no font: a 5x7 ASCII table is
 * 480 bytes of flash and a lot of hand-entered hex to get subtly wrong.
 *
 * Every draw is into a framebuffer with per-page dirty tracking, and only pages
 * that actually changed go to the panel. Phase 0 measured a full frame at
 * 12.78 ms of bus against 3.23 ms for one page, and since the artefact that
 * makes the display worth thinking about at all is analog and scales with how
 * long the bus is busy, redrawing less is the only lever software has.
 */

#ifndef DISPLAY_H
#define DISPLAY_H

#include <stdbool.h>
#include <stdint.h>

/* 400 kHz, the SSD1306 datasheet maximum.
 *
 * Phase 0 measured 1 MHz at 5.58 ms a frame against 12.78 - a real 2.3x - and
 * saw it run clean over forty transfers. It is still out of spec, and forty
 * transfers is not a soak, so the in-spec speed is the default and the faster
 * one is a single edit here once somebody has listened to both. */
#define OLED_BAUDRATE 400000

void display_init(void);
void display_clear(void);

void display_pixel(uint32_t x, uint32_t y, bool on);
void display_rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, bool on);
void display_fill_rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, bool on);

/* Send the pages that changed. Returns how many were written, which is the
 * number that decides what this cost. */
uint32_t display_flush(void);

#endif /* DISPLAY_H */
