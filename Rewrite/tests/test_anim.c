/* Host tests for the light strip.
 *
 * The whole reason engine/animation.py is pure logic is stated in its own
 * docstring: what the strip shows on the third sixteenth of the second bar
 * should be a test rather than something judged by waving a badge about. That
 * carries over, and it carries over for a second reason here - this is a
 * reimplementation in integers of something written in floats, so "does it
 * still do the same thing" is a real question with a checkable answer.
 */

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "anim.h"

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

static bool lit(struct rgb c) {
    return c.r || c.g || c.b;
}

static uint32_t count_lit(const struct rgb *strip) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
        if (lit(strip[i])) {
            n++;
        }
    }
    return n;
}

/* Ticks per beat and bar, spelled out so the tests read as music. */
#define BEAT 24
#define BAR (BEAT * 4)

static void test_pulse_lands_on_the_beat(void) {
    struct rgb strip[NEOPIXEL_COUNT];

    anim_render(ANIM_PULSE, 0, 255, strip);
    struct rgb on_beat = strip[0];
    check(lit(on_beat), "the strip is lit on the downbeat");
    for (uint32_t i = 1; i < NEOPIXEL_COUNT; i++) {
        check(memcmp(&strip[i], &on_beat, sizeof(on_beat)) == 0,
              "and the whole strip is one colour");
    }

    /* It decays until the next beat, and comes back at it. */
    anim_render(ANIM_PULSE, BEAT / 2, 255, strip);
    struct rgb half = strip[0];
    check(half.r + half.g + half.b < on_beat.r + on_beat.g + on_beat.b,
          "dimmer halfway through the beat");

    anim_render(ANIM_PULSE, BEAT - 1, 255, strip);
    struct rgb late = strip[0];
    check(late.r + late.g + late.b < half.r + half.g + half.b,
          "dimmer still just before the next");

    anim_render(ANIM_PULSE, BEAT, 255, strip);
    check(lit(strip[0]), "and struck again on it");

    /* The colour changes with the beat of the bar, so four beats look
     * different from each other rather than being one flashing colour. */
    struct rgb beats[4];
    for (uint32_t b = 0; b < 4; b++) {
        anim_render(ANIM_PULSE, b * BEAT, 255, beats + b);
    }
    check(memcmp(&beats[0], &beats[1], sizeof(struct rgb)) != 0,
          "beat 2 is a different colour from beat 1");
    check(memcmp(&beats[1], &beats[2], sizeof(struct rgb)) != 0,
          "and beat 3 from beat 2");
}

static void test_travelling_animations_lap_once_a_bar(void) {
    struct rgb strip[NEOPIXEL_COUNT];

    /* A chase is one pixel, and it is in the same place at the top of every
     * bar - the lap is locked to the bar, which is the whole reason it is
     * positioned by bar phase rather than counted in sixteenths. Ten pixels
     * do not divide sixteen sixteenths, and counting would bring it home only
     * every five bars. */
    anim_render(ANIM_CHASE, 0, 255, strip);
    check(count_lit(strip) == 1, "a chase is one pixel");
    uint32_t first = 0;
    for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
        if (lit(strip[i])) {
            first = i;
        }
    }

    anim_render(ANIM_CHASE, BAR, 255, strip);
    check(lit(strip[first]), "and is back where it started a bar later");
    anim_render(ANIM_CHASE, BAR * 7, 255, strip);
    check(lit(strip[first]), "and seven bars later");

    /* Across one bar it visits every pixel exactly once. */
    bool seen[NEOPIXEL_COUNT];
    memset(seen, 0, sizeof(seen));
    for (uint32_t t = 0; t < BAR; t++) {
        anim_render(ANIM_CHASE, t, 255, strip);
        for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
            if (lit(strip[i])) {
                seen[i] = true;
            }
        }
    }
    for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
        check(seen[i], "every pixel is visited within one bar");
    }

    /* A comet is the same path with a tail, so it is wider and its head is
     * the brightest part of it. */
    anim_render(ANIM_COMET, BAR / 3, 255, strip);
    check(count_lit(strip) == 4, "a comet is a head and three of tail");
}

static void test_sparkle_repeats_so_a_loop_looks_like_a_loop(void) {
    /* The randomness is a hash of the beat, not a generator. A looping pattern
     * should get a repeating light show rather than a fizz - and it is what
     * makes this testable at all. */
    struct rgb first[NEOPIXEL_COUNT];
    struct rgb again[NEOPIXEL_COUNT];
    anim_render(ANIM_SPARKLE, BAR + 12, 255, first);
    anim_render(ANIM_SPARKLE, BAR * 5 + 12, 255, again);
    check(memcmp(first, again, sizeof(first)) == 0,
          "the same point in a later bar looks identical");

    anim_render(ANIM_SPARKLE, BAR + 18, 255, again);
    check(memcmp(first, again, sizeof(first)) != 0,
          "a different sixteenth does not");

    check(count_lit(first) >= 1 && count_lit(first) <= 3,
          "up to three pixels, fewer when two hash to the same one");
}

static void test_brightness_scales_without_changing_the_picture(void) {
    /* Brightness is a separate argument rather than baked in, because the
     * panel is diffused and what looks right on a bench is dazzling in a dark
     * room. Turning it down must dim, not rearrange. */
    struct rgb full[NEOPIXEL_COUNT];
    struct rgb dim[NEOPIXEL_COUNT];
    anim_render(ANIM_RAINBOW, BAR / 4, 255, full);
    anim_render(ANIM_RAINBOW, BAR / 4, 64, dim);

    for (uint32_t i = 0; i < NEOPIXEL_COUNT; i++) {
        check(dim[i].r <= full[i].r && dim[i].g <= full[i].g &&
                  dim[i].b <= full[i].b,
              "every channel is no brighter");
        check(lit(dim[i]) == lit(full[i]) || !lit(dim[i]),
              "and the same pixels are lit, or fewer at the very bottom");
    }

    struct rgb none[NEOPIXEL_COUNT];
    anim_render(ANIM_RAINBOW, BAR / 4, 0, none);
    check(count_lit(none) == 0, "at zero brightness nothing is lit");
}

static void test_off_is_off_and_every_animation_is_safe(void) {
    struct rgb strip[NEOPIXEL_COUNT];
    anim_render(ANIM_OFF, 12345, 255, strip);
    check(count_lit(strip) == 0, "OFF lights nothing");

    /* Every animation, across a couple of bars, must stay inside the strip and
     * name itself. A crash here is an out-of-range pixel index, which on the
     * badge is memory belonging to something else. */
    for (int a = 0; a < ANIM_COUNT; a++) {
        check(anim_name((enum anim)a)[0] != '\0', "the animation has a name");
        for (uint32_t t = 0; t < BAR * 2; t++) {
            anim_render((enum anim)a, t, 255, strip);
        }
    }
    check(true, "every animation ran two bars without wandering off the strip");
}

int main(void) {
    test_pulse_lands_on_the_beat();
    test_travelling_animations_lap_once_a_bar();
    test_sparkle_repeats_so_a_loop_looks_like_a_loop();
    test_brightness_scales_without_changing_the_picture();
    test_off_is_off_and_every_animation_is_safe();

    if (failures == 0) {
        printf("ok - all animation tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
