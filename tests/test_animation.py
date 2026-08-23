"""Tests for the beat-synced LED animations.

engine.animation imports nothing from CircuitPython and takes a tick rather
than a wall clock, so what the strip shows on a given sixteenth is checked
here rather than by waving a badge about.

The point of the rework is that these follow the music. The library
animations they replace ran on elapsed time, so a chase at 0.1 s a step
matched a 120 BPM pattern only by accident and any other tempo not at all.
Several tests below exist purely to pin that down.
"""

import pytest

from engine.animation import (
    ANIMATIONS,
    BEATS_PER_BAR,
    COLUMNS,
    INDICATORS,
    PATH,
    LOWER,
    NAMES,
    OFF,
    PADS,
    PIXEL_COUNT,
    TICKS_PER_BAR,
    TICKS_PER_BEAT,
    TICKS_PER_SIXTEENTH,
    UPPER,
    bar_phase,
    beat_of_bar,
    beat_phase,
    by_name,
    chase,
    comet,
    dim,
    free_running_tick,
    heartbeat,
    off,
    pulse,
    rainbow,
    sixteenth,
    sparkle,
    sweep,
    wheel,
)
from engine.clock import PPQN

# --- the geometry ---------------------------------------------------------


def test_the_pad_pixels_agree_with_the_measured_key_mapping():
    """The one table nobody can derive from the boards.

    utils.neoindex was established by lighting each pixel on the badge and
    writing down what was underneath it; two earlier versions derived from
    the CAD were both wrong. If these two ever disagree, an animation lights
    a different pad from the one the sampler lights.
    """
    from utils import neoindex

    for pad in range(8):
        assert PADS[pad] == neoindex(pad), "pad %d" % (pad + 1)


def test_the_button_pixels_agree_with_the_measured_key_mapping():
    from utils import neoindex

    play, function = INDICATORS[1], INDICATORS[0]
    assert play == neoindex(8), "Play"
    assert function == neoindex(9), "Function"


def test_every_pixel_is_accounted_for_exactly_once():
    assert sorted(PADS + INDICATORS) == list(range(PIXEL_COUNT))


def test_the_rows_are_the_two_halves_of_the_grid():
    assert UPPER + LOWER == PADS
    assert len(UPPER) == len(LOWER) == 4


def test_a_column_is_the_pad_above_and_the_pad_below():
    assert len(COLUMNS) == 4
    for index, column in enumerate(COLUMNS):
        assert column == (UPPER[index], LOWER[index])


# --- time -----------------------------------------------------------------


def test_a_beat_is_a_quarter_of_a_bar():
    assert TICKS_PER_BAR == TICKS_PER_BEAT * BEATS_PER_BAR
    assert TICKS_PER_BEAT == PPQN


def test_the_beat_phase_starts_at_zero_and_never_reaches_one():
    assert beat_phase(0) == 0.0
    assert 0.0 < beat_phase(1) < 1.0
    assert beat_phase(TICKS_PER_BEAT) == 0.0


def test_the_beat_phase_is_half_way_at_the_half_beat():
    assert beat_phase(TICKS_PER_BEAT // 2) == pytest.approx(0.5)


def test_the_bar_phase_walks_the_four_beats():
    assert bar_phase(0) == 0.0
    assert bar_phase(TICKS_PER_BEAT * 2) == pytest.approx(0.5)
    assert bar_phase(TICKS_PER_BAR) == 0.0


def test_the_beat_of_the_bar_counts_round():
    for beat in range(BEATS_PER_BAR):
        assert beat_of_bar(TICKS_PER_BEAT * beat) == beat
    assert beat_of_bar(TICKS_PER_BAR) == 0


def test_sixteenths_only_increase():
    assert sixteenth(0) == 0
    assert sixteenth(TICKS_PER_SIXTEENTH) == 1
    assert sixteenth(TICKS_PER_BAR) == 16


# --- colour ---------------------------------------------------------------


def test_the_wheel_is_a_closed_loop():
    assert wheel(0) == wheel(256)


def test_the_wheel_never_leaves_the_byte_range():
    for position in range(0, 256):
        for channel in wheel(position):
            assert 0 <= channel <= 255, position


def test_a_negative_or_huge_hue_still_lands_on_the_wheel():
    """Callers pass a phase times 255, which is a float and can overrun."""
    assert wheel(-1) == wheel(255)
    assert wheel(1000) == wheel(1000 % 256)


def test_dimming_to_nothing_is_off():
    assert dim((255, 255, 255), 0.0) == OFF
    assert dim((255, 255, 255), -1.0) == OFF


def test_dimming_past_full_does_not_overflow():
    assert dim((255, 255, 255), 4.0) == (255, 255, 255)


def test_dimming_half_is_half():
    assert dim((200, 100, 50), 0.5) == (100, 50, 25)


# --- every animation ------------------------------------------------------


ALL = [function for _label, function in ANIMATIONS]


@pytest.mark.parametrize("animation", ALL)
def test_an_animation_lights_the_whole_strip_and_no_more(animation):
    colors = animation(0)
    assert len(colors) == PIXEL_COUNT


@pytest.mark.parametrize("animation", ALL)
def test_an_animation_only_produces_colours_a_neopixel_can_show(animation):
    """A channel outside 0-255 raises on hardware, mid-animation."""
    for tick in range(TICKS_PER_BAR * 2):
        for color in animation(tick):
            assert len(color) == 3
            for channel in color:
                assert isinstance(channel, int)
                assert 0 <= channel <= 255, (animation.__name__, tick, color)


@pytest.mark.parametrize("animation", ALL)
def test_an_animation_repeats_every_bar(animation):
    """Locked to the music means a bar looks like the bar before it."""
    for tick in range(TICKS_PER_BAR):
        assert animation(tick) == animation(tick + TICKS_PER_BAR), tick


@pytest.mark.parametrize("animation", ALL)
def test_brightness_turns_an_animation_down(animation):
    if animation is off:
        pytest.skip("nothing to dim")
    bright = sum(
        sum(color) for tick in range(TICKS_PER_BAR) for color in animation(tick)
    )
    dark = sum(
        sum(color) for tick in range(TICKS_PER_BAR) for color in animation(tick, 0.25)
    )
    assert dark < bright


@pytest.mark.parametrize("animation", ALL)
def test_an_animation_at_a_negative_tick_still_works(animation):
    """The free-running tick is derived from a wrapping millisecond counter."""
    for color in animation(-5):
        for channel in color:
            assert 0 <= channel <= 255


# --- what each one actually does -----------------------------------------


def test_the_pulse_is_brightest_on_the_beat():
    on_beat = sum(pulse(0)[0])
    later = sum(pulse(TICKS_PER_BEAT // 2)[0])
    assert on_beat > later


def test_the_pulse_decays_across_the_beat():
    levels = [sum(pulse(tick)[0]) for tick in range(TICKS_PER_BEAT)]
    assert levels == sorted(levels, reverse=True)


def test_the_pulse_hits_every_pixel_at_once():
    colors = pulse(0)
    assert len(set(colors)) == 1


def test_the_pulse_changes_colour_each_beat_of_the_bar():
    seen = {pulse(TICKS_PER_BEAT * beat)[0] for beat in range(BEATS_PER_BAR)}
    assert len(seen) == BEATS_PER_BAR


def test_the_chase_lights_exactly_one_pixel():
    lit = [color for color in chase(0) if color != OFF]
    assert len(lit) == 1


def _lit(colors):
    return [index for index, color in enumerate(colors) if color != OFF]


def test_the_chase_moves_one_pad_a_sixteenth():
    """Along the pads in reading order, not along the strip's wiring."""
    for step in range(len(PATH) - 1):
        here = _lit(chase(TICKS_PER_SIXTEENTH * step))[0]
        there = _lit(chase(TICKS_PER_SIXTEENTH * (step + 1)))[0]
        assert (PATH.index(here), PATH.index(there)) == (step, step + 1)


def test_the_chase_goes_round_twice_in_a_bar():
    """Eight pads into sixteen sixteenths. Ten pixels would not divide.

    The position comes back, not the whole frame: the hue walks the bar in
    sixteen steps, so the second lap is the same path in another colour.
    """
    assert _lit(chase(0)) == _lit(chase(TICKS_PER_SIXTEENTH * len(PATH)))
    assert chase(0) != chase(TICKS_PER_SIXTEENTH * len(PATH)), "the colour stood still"


def test_the_chase_leaves_the_buttons_alone():
    for tick in range(TICKS_PER_BAR):
        for pixel in INDICATORS:
            assert chase(tick)[pixel] == OFF


def test_the_chase_does_not_change_at_all_inside_a_sixteenth():
    """Including its colour: a still dot whose hue slides looks like a fault."""
    assert chase(0) == chase(TICKS_PER_SIXTEENTH - 1)


def test_the_comet_has_a_tail_behind_its_head():
    assert len(_lit(comet(TICKS_PER_SIXTEENTH * 5, tail=4))) == 4


def test_the_comet_tail_fades_away_from_the_head():
    head = 5
    colors = comet(TICKS_PER_SIXTEENTH * head, tail=4)
    levels = [sum(colors[PATH[(head - step) % len(PATH)]]) for step in range(4)]
    assert levels == sorted(levels, reverse=True)


def test_the_comet_leaves_the_buttons_alone():
    for tick in range(TICKS_PER_BAR):
        for pixel in INDICATORS:
            assert comet(tick)[pixel] == OFF


def test_the_sweep_travels_across_the_columns_and_back():
    """Left to right in real space, not along the snake the wiring is."""

    def brightest_column(tick):
        colors = sweep(tick)
        totals = [sum(sum(colors[pixel]) for pixel in column) for column in COLUMNS]
        return totals.index(max(totals))

    assert brightest_column(0) == 0
    assert brightest_column(TICKS_PER_BAR // 2) == len(COLUMNS) - 1
    assert brightest_column(TICKS_PER_BAR - 1) == 0


def test_the_sweep_lights_a_column_top_and_bottom_together():
    colors = sweep(0)
    upper, lower = COLUMNS[0]
    assert colors[upper] == colors[lower]


def test_the_sweep_leaves_the_buttons_alone():
    """They mean something; a sweep running over them reads as state."""
    for tick in range(TICKS_PER_BAR):
        colors = sweep(tick)
        for pixel in INDICATORS:
            assert colors[pixel] == OFF


def test_the_rainbow_shows_a_different_hue_on_each_pixel():
    assert len(set(rainbow(0))) == PIXEL_COUNT


def test_the_rainbow_turns_once_a_bar():
    assert rainbow(0) != rainbow(TICKS_PER_BAR // 2)
    assert rainbow(0) == rainbow(TICKS_PER_BAR)


def test_the_sparkle_repeats_with_the_bar():
    """A loop should get a repeating light show rather than a fizz."""
    assert sparkle(TICKS_PER_SIXTEENTH * 3) == sparkle(
        TICKS_PER_SIXTEENTH * 3 + TICKS_PER_BAR
    )


def test_the_sparkle_is_the_same_every_time_for_a_given_beat():
    """A hash of the beat rather than random(), so a loop repeats."""
    assert sparkle(TICKS_PER_SIXTEENTH * 3) == sparkle(TICKS_PER_SIXTEENTH * 3)


def test_the_sparkle_changes_every_sixteenth():
    assert sparkle(0) != sparkle(TICKS_PER_SIXTEENTH)


def test_the_sparkle_does_not_move_inside_a_sixteenth():
    assert sparkle(0) == sparkle(TICKS_PER_SIXTEENTH - 1)


def test_the_sparkle_lights_at_most_what_was_asked_for():
    lit = [color for color in sparkle(0, count=3) if color != OFF]
    assert len(lit) <= 3


def test_the_heartbeat_beats_twice_a_bar():
    """On beats one and three, which is what a bar of four feels like."""

    def loud(beat):
        return sum(sum(color) for color in heartbeat(TICKS_PER_BEAT * beat))

    assert loud(0) > 0
    assert loud(1) == 0
    assert loud(2) > 0
    assert loud(3) == 0


def test_the_heartbeat_leaves_the_buttons_alone():
    for tick in range(TICKS_PER_BAR):
        for pixel in INDICATORS:
            assert heartbeat(tick)[pixel] == OFF


def test_off_is_dark_at_every_tick():
    for tick in range(TICKS_PER_BAR):
        assert off(tick) == [OFF] * PIXEL_COUNT


# --- the registry ---------------------------------------------------------


def test_every_animation_has_a_name():
    assert len(NAMES) == len(ANIMATIONS)
    assert len(set(NAMES)) == len(NAMES), "two animations share a name"


def test_a_name_fits_the_display():
    """21 columns, less the cursor and the scroll marker."""
    for name in NAMES:
        assert len(name) <= 19, name


def test_looking_one_up_returns_it():
    for name, function in ANIMATIONS:
        assert by_name(name) is function


def test_an_unknown_name_falls_back_rather_than_raising():
    """A saved setting from a firmware that had another animation."""
    assert by_name("nonexistent") is pulse


# --- the free-running clock ----------------------------------------------


def test_the_free_running_tick_matches_the_tempo():
    """One beat of ticks after one beat of milliseconds at that tempo."""
    one_beat_ms = 60000.0 / 120
    assert free_running_tick(one_beat_ms, 120) == TICKS_PER_BEAT


def test_a_faster_tempo_runs_the_lights_faster():
    assert free_running_tick(1000, 240) > free_running_tick(1000, 120)


def test_a_zero_tempo_does_not_divide_by_it():
    assert free_running_tick(1000, 0) == 0
