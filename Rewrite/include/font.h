/* A 6x8 bitmap font, and the text it draws.
 *
 * Phase 1 got away without one: blocks and bars suit a playing screen, need no
 * glyph table, and avoid 95 characters of hand-entered hex. A menu cannot -
 * the whole point of it is showing names.
 *
 * Departure Mono was tried and rejected, and it is worth saying why so nobody
 * has to try it twice. It is a genuine pixel font and renders beautifully - a
 * real crossbar on the A, a real V in the M, none of the mush a thresholded
 * scalable face gives at this size. But it is correct only at its native 11 px,
 * which trims to a 7x11 cell: two rows of eighteen characters on a 128x32
 * panel, against four rows of twenty-one here. Too tall for this display.
 *
 * It also does not fit this table. Eleven rows need eleven bits per column and
 * these are bytes, so the values silently truncated - 382 became 126 - and the
 * font would have shipped corrupted rather than merely large. Worth keeping in
 * mind if a taller cell is ever wanted: the storage has to change with it.
 */

#ifndef FONT_H
#define FONT_H

#include <stdbool.h>
#include <stdint.h>

#define FONT_WIDTH 6
#define FONT_HEIGHT 8
#define FONT_FIRST 0x20
#define FONT_LAST 0x7E
#define FONT_GLYPHS (FONT_LAST - FONT_FIRST + 1)

/* One byte per column, least significant bit at the top - the order the
 * SSD1306 stores a page in, so drawing is a copy rather than a transpose. */
extern const uint8_t FONT_6X8[FONT_GLYPHS][FONT_WIDTH];

/* Draw text at a pixel position. Returns the x it finished at, so callers can
 * chain without measuring. Clips rather than wrapping: a name too long for the
 * screen should be cut off, not rearranged. */
uint32_t display_text(uint32_t x, uint32_t y, const char *text, bool on);

/* How wide `text` would be, for centring and for deciding what fits. */
uint32_t display_text_width(const char *text);

#endif /* FONT_H */
