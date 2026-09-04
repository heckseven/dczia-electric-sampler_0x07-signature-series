/* See anim.h. */

#include <stdbool.h>
#include <string.h>

#include "anim.h"
#include "song.h" /* PPQN */

#define TICKS_PER_BEAT PPQN
#define BEATS_PER_BAR 4
#define TICKS_PER_BAR (PPQN * BEATS_PER_BAR)
#define TICKS_PER_SIXTEENTH (PPQN / 4)
#define SIXTEENTHS_PER_BAR (TICKS_PER_BAR / TICKS_PER_SIXTEENTH)

/* Phases are 0-256, not 0.0-1.0. 256 rather than 255 so that dividing by it is
 * a shift and a phase of "all the way round" is exactly one unit past zero. */
#define PHASE_ONE 256

/* What anything travelling goes round: a serpentine over the panel, left to
 * right along the buttons, back along the top pad row, forward along the
 * bottom. Not strip order - the two button pixels are numbered right to left
 * against the panel (pixel 1 is Play, on the left; pixel 0 is Function, to its
 * right), so strip order would make a chase jump sideways. */
static const uint8_t PATH[] = {1, 0, 2, 3, 4, 5, 6, 7, 8, 9};
#define PATH_LEN (sizeof(PATH) / sizeof(PATH[0]))

/* Left to right across the panel. The first two columns have a button on top;
 * the other two are pads only. What a sweep travels along. */
static const uint8_t COLUMNS[4][3] = {
    {1, 5, 6},
    {0, 4, 7},
    {3, 8, 0xFF},
    {2, 9, 0xFF},
};
#define COLUMN_COUNT 4

static const struct rgb OFF_COLOR = {0, 0, 0};
static const struct rgb SCANNER_RED = {255, 0, 0};
/* How brightly the column the eye has just left still glows: enough to say
 * which way it is going, far enough down not to widen the eye. 0.22 of full. */
#define SCANNER_TRAIL 56

/* --- time ------------------------------------------------------------------ */

static uint32_t beat_phase(uint64_t tick) {
    return (uint32_t)((tick % TICKS_PER_BEAT) * PHASE_ONE / TICKS_PER_BEAT);
}

static uint32_t bar_phase(uint64_t tick) {
    return (uint32_t)((tick % TICKS_PER_BAR) * PHASE_ONE / TICKS_PER_BAR);
}

static uint32_t beat_of_bar(uint64_t tick) {
    return (uint32_t)((tick / TICKS_PER_BEAT) % BEATS_PER_BAR);
}

static uint32_t sixteenth(uint64_t tick) {
    return (uint32_t)(tick / TICKS_PER_SIXTEENTH);
}

/* --- colour ---------------------------------------------------------------- */

struct rgb anim_wheel(uint32_t hue) {
    struct rgb c;
    hue %= 256u;
    if (hue < 85u) {
        c.r = (uint8_t)(255u - hue * 3u);
        c.g = (uint8_t)(hue * 3u);
        c.b = 0;
    } else if (hue < 170u) {
        hue -= 85u;
        c.r = 0;
        c.g = (uint8_t)(255u - hue * 3u);
        c.b = (uint8_t)(hue * 3u);
    } else {
        hue -= 170u;
        c.r = (uint8_t)(hue * 3u);
        c.g = 0;
        c.b = (uint8_t)(255u - hue * 3u);
    }
    return c;
}

/* Scale a colour. `level` is 0-256 and is clamped. */
static struct rgb dim(struct rgb c, int32_t level) {
    if (level <= 0) {
        return OFF_COLOR;
    }
    if (level > PHASE_ONE) {
        level = PHASE_ONE;
    }
    struct rgb out;
    out.r = (uint8_t)((c.r * level) / PHASE_ONE);
    out.g = (uint8_t)((c.g * level) / PHASE_ONE);
    out.b = (uint8_t)((c.b * level) / PHASE_ONE);
    return out;
}

static void blank(struct rgb *out) {
    memset(out, 0, sizeof(struct rgb) * NEOPIXEL_COUNT);
}

static void fill(struct rgb *out, struct rgb c) {
    for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
        out[i] = c;
    }
}

/* Where a travelling animation is, as an index into PATH.
 *
 * Taken from the position in the bar rather than counted in sixteenths, so a
 * lap is exactly one bar however many pixels the path has. Ten does not divide
 * the sixteen sixteenths of a bar, and counting would bring it back to the top
 * only every five bars. */
static uint32_t travelling_position(uint64_t tick) {
    return (bar_phase(tick) * PATH_LEN / PHASE_ONE) % PATH_LEN;
}

static void paint_column(struct rgb *out, uint32_t column, struct rgb c) {
    for (uint32_t i = 0; i < 3; i++) {
        uint8_t pixel = COLUMNS[column][i];
        if (pixel != 0xFF) {
            out[pixel] = c;
        }
    }
}

/* --- the animations -------------------------------------------------------- */

void anim_render(enum anim which, uint64_t tick, uint8_t brightness,
                 struct rgb out[NEOPIXEL_COUNT]) {
    int32_t bright = brightness + 1; /* 0-255 in, 0-256 as a level */

    switch (which) {
    case ANIM_PULSE: {
        /* The whole strip hit on the beat, decaying until the next one. The
         * plainest statement of the tempo, and the one to check a sync lead
         * against: if this is not landing with the kick, nothing else will. */
        int32_t remaining = PHASE_ONE - (int32_t)beat_phase(tick);
        int32_t level = (remaining * remaining) / PHASE_ONE;
        fill(out, dim(anim_wheel(beat_of_bar(tick) * 64u),
                      (level * bright) / PHASE_ONE));
        break;
    }

    case ANIM_CHASE: {
        blank(out);
        out[PATH[travelling_position(tick)]] =
            dim(anim_wheel(bar_phase(tick) * 255u / PHASE_ONE), bright);
        break;
    }

    case ANIM_COMET: {
        /* A chase with a tail, so the direction reads at speed. */
        const int32_t tail = 4;
        uint32_t head = travelling_position(tick);
        uint32_t hue = bar_phase(tick) * 255u / PHASE_ONE;
        blank(out);
        for (int32_t step = 0; step < tail; step++) {
            uint32_t at = (head + PATH_LEN - (uint32_t)step) % PATH_LEN;
            int32_t level = bright * (tail - step) / tail;
            out[PATH[at]] = dim(anim_wheel(hue), level);
        }
        break;
    }

    case ANIM_SWEEP: {
        /* A column of light crossing the panel and back, once a bar. Travels
         * in real space rather than along the strip, so it reads as left to
         * right movement rather than as the snake the wiring actually is. */
        int32_t phase = (int32_t)(bar_phase(tick) * 2u);
        if (phase > PHASE_ONE) {
            phase = 2 * PHASE_ONE - phase;
        }
        int32_t position = phase * (COLUMN_COUNT - 1); /* in units of 256 */
        uint32_t hue = beat_of_bar(tick) * 64u;
        blank(out);
        for (int32_t i = 0; i < COLUMN_COUNT; i++) {
            int32_t distance = i * PHASE_ONE - position;
            if (distance < 0) {
                distance = -distance;
            }
            if (distance >= PHASE_ONE) {
                continue;
            }
            int32_t level = ((PHASE_ONE - distance) * bright) / PHASE_ONE;
            paint_column(out, (uint32_t)i, dim(anim_wheel(hue), level));
        }
        break;
    }

    case ANIM_RAINBOW: {
        uint32_t base = bar_phase(tick) * 255u / PHASE_ONE;
        for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
            out[i] = dim(anim_wheel(base + i * 255u / NEOPIXEL_COUNT), bright);
        }
        break;
    }

    case ANIM_SPARKLE: {
        /* Pixels lit at random, redrawn every sixteenth. The randomness is a
         * hash of the beat rather than a generator, so the same bar looks the
         * same twice - which makes it testable, and means a looping pattern
         * gets a repeating light show rather than a fizz. */
        blank(out);
        uint32_t seed = sixteenth(tick) % SIXTEENTHS_PER_BAR;
        for (uint32_t i = 0; i < 3; i++) {
            uint32_t value = seed * 2654435761u + i * 40503u;
            out[(value >> 8) % NEOPIXEL_COUNT] =
                dim(anim_wheel((value >> 16) & 0xFFu), bright);
        }
        break;
    }

    case ANIM_HEARTBEAT: {
        /* Two quick hits a beat apart in the bar, like a pulse taken by hand.
         * Quieter than the others: the one to leave running while doing
         * something else on the badge. */
        int32_t phase = (int32_t)beat_phase(tick);
        uint32_t beat = beat_of_bar(tick);
        int32_t level = 0;
        if (beat == 0 || beat == 2) {
            if (phase < 46) { /* 0.18 */
                level = PHASE_ONE - (phase * PHASE_ONE) / 46;
            } else if (phase >= 64 && phase < 102) { /* 0.25 to 0.40 */
                level = (PHASE_ONE * 6 / 10) *
                        (PHASE_ONE - ((phase - 64) * PHASE_ONE) / 38) /
                        PHASE_ONE;
            }
        }
        struct rgb blood = {180, 0, 40};
        fill(out, dim(blood, (level * bright) / PHASE_ONE));
        break;
    }

    case ANIM_TOASTER: {
        /* A red eye sweeping the panel and back. One column is the eye and
         * the column it has just left carries a dim trail, which is the whole
         * of it. Four positions is too few to draw a soft edge on: falling off
         * smoothly lit three of the four at once, which is not an eye, it is a
         * red panel. */
        int32_t phase = (int32_t)(bar_phase(tick) * 4u) % (2 * PHASE_ONE);
        bool forward = phase <= PHASE_ONE;
        if (!forward) {
            phase = 2 * PHASE_ONE - phase;
        }
        int32_t position = phase * (COLUMN_COUNT - 1);
        int32_t eye = (position + PHASE_ONE / 2) / PHASE_ONE;
        int32_t behind = forward ? eye - 1 : eye + 1;
        blank(out);
        for (int32_t i = 0; i < COLUMN_COUNT; i++) {
            int32_t level;
            if (i == eye) {
                level = bright;
            } else if (i == behind) {
                level = (SCANNER_TRAIL * bright) / PHASE_ONE;
            } else {
                continue;
            }
            paint_column(out, (uint32_t)i, dim(SCANNER_RED, level));
        }
        break;
    }

    case ANIM_OFF:
    default:
        blank(out);
        break;
    }
}

const char *anim_name(enum anim which) {
    switch (which) {
    case ANIM_OFF:
        return "OFF";
    case ANIM_PULSE:
        return "PULSE";
    case ANIM_CHASE:
        return "CHASE";
    case ANIM_COMET:
        return "COMET";
    case ANIM_SWEEP:
        return "SWEEP";
    case ANIM_RAINBOW:
        return "RAINBOW";
    case ANIM_SPARKLE:
        return "SPARKLE";
    case ANIM_HEARTBEAT:
        return "HEARTBEAT";
    case ANIM_TOASTER:
        return "TOASTER";
    default:
        return "?";
    }
}
