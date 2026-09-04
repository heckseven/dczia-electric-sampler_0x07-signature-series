/* Lights that move with the beat.
 *
 * Reproduced from engine/animation.py, whose opening note is the reason any of
 * this exists: the library animations it replaced ran on a wall clock, so a
 * chase at 0.1 s a step drifted against a 120 BPM pattern and matched nothing.
 * Everything here is a function of the sequencer's tick instead.
 *
 * Which buys something for free. The transport already latches to an external
 * clock and flywheels through gaps in it, so these follow a drum machine
 * plugged into the sync input without knowing that is what happened.
 *
 * Pure logic - a tick goes in, ten colours come out - so what the strip shows
 * on the third sixteenth of the second bar is a test rather than something
 * judged by waving a badge about.
 *
 * Integers throughout, where the Python uses floats. Phases are held as 0-256
 * rather than 0.0-1.0: this chip emulates floating point in software, and there
 * is no reason for the strip to spend cycles the mixer might want.
 */

#ifndef ANIM_H
#define ANIM_H

#include <stdint.h>

#include "board.h"

struct rgb {
    uint8_t r, g, b;
};

enum anim {
    ANIM_OFF = 0,
    ANIM_PULSE,
    ANIM_CHASE,
    ANIM_COMET,
    ANIM_SWEEP,
    ANIM_RAINBOW,
    ANIM_SPARKLE,
    ANIM_HEARTBEAT,
    ANIM_TOASTER,
    ANIM_COUNT,
};

/* Ten colours for a tick. `brightness` is 0-255 and is a separate argument
 * rather than baked in, because the panel is diffused and what looks right on a
 * bench is dazzling in a dark room. */
void anim_render(enum anim which, uint64_t tick, uint8_t brightness,
                 struct rgb out[NEOPIXEL_COUNT]);

const char *anim_name(enum anim which);

/* A colour from a hue 0-255, full saturation. Exposed because the pad colours
 * want it too. */
struct rgb anim_wheel(uint32_t hue);

#endif /* ANIM_H */
