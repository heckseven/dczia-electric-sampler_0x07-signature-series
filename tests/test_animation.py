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
    BIRD_FALL,
    ANIMATIONS,
    BEATS_PER_BAR,
    COLUMNS,
    FUNCTION_PIXEL,
    INDICATORS,
    PATH,
    PLAY_PIXEL,
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
    MAX_STEP_MS,
    Timebase,
    heartbeat,
    off,
    pulse,
    rainbow,
    sixteenth,
    sparkle,
    sweep,
    toaster,
    seven,
    bird,
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

    assert PLAY_PIXEL == neoindex(8), "Play"
    assert FUNCTION_PIXEL == neoindex(9), "Function"


def test_the_buttons_are_listed_left_to_right():
    """Play is the left one.

    Settled from the board rather than guessed: SW9 (Play) sits at x=123.71
    and SW10 (Function) at x=142.76, the same two x positions as pads 1 and
    2 below them. The pixel numbers run the other way, so the leftmost
    position on the panel is pixel 1.
    """
    assert INDICATORS == (PLAY_PIXEL, FUNCTION_PIXEL)
    assert PATH[0] == PLAY_PIXEL, "a chase should start at the left"


def test_every_pixel_is_accounted_for_exactly_once():
    assert sorted(PADS + INDICATORS) == list(range(PIXEL_COUNT))


def test_the_rows_are_the_two_halves_of_the_grid():
    assert UPPER + LOWER == PADS
    assert len(UPPER) == len(LOWER) == 4


def test_a_column_runs_down_the_panel():
    """Four columns. The left two carry a button above the pads as well."""
    assert len(COLUMNS) == 4
    assert COLUMNS[0] == (PLAY_PIXEL, UPPER[0], LOWER[0])
    assert COLUMNS[1] == (FUNCTION_PIXEL, UPPER[1], LOWER[1])
    for index in (2, 3):
        assert COLUMNS[index] == (UPPER[index], LOWER[index])


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


def test_the_chase_goes_once_round_the_panel_in_a_bar():
    """Every pixel, buttons included, one lap per bar.

    Positioned by where it is in the bar rather than counted in sixteenths,
    because ten pixels do not divide sixteen sixteenths - a stepped version
    would only come back to the top every five bars.
    """
    seen = []
    for tick in range(TICKS_PER_BAR):
        lit = _lit(chase(tick))
        if lit and (not seen or lit[0] != seen[-1]):
            seen.append(lit[0])
    assert sorted(seen) == sorted(PATH), seen


def test_the_chase_starts_a_new_lap_each_bar():
    assert _lit(chase(0)) == _lit(chase(TICKS_PER_BAR))


def test_the_chase_covers_the_buttons_too():
    """They are part of the panel; an animation that skips them looks broken."""
    lit_somewhere = set()
    for tick in range(TICKS_PER_BAR):
        lit_somewhere.update(_lit(chase(tick)))
    for pixel in INDICATORS:
        assert pixel in lit_somewhere, pixel


def test_the_comet_has_a_tail_behind_its_head():
    assert len(_lit(comet(TICKS_PER_BAR // 2, tail=4))) == 4


def test_the_comet_tail_fades_away_from_the_head():
    tick = TICKS_PER_BAR // 2
    colors = comet(tick, tail=4)
    head = int((tick % TICKS_PER_BAR) / TICKS_PER_BAR * len(PATH))
    levels = [sum(colors[PATH[(head - step) % len(PATH)]]) for step in range(4)]
    assert levels == sorted(levels, reverse=True)


def test_the_sweep_travels_across_the_columns_and_back():
    """Left to right in real space, not along the snake the wiring is."""

    def brightest_column(tick):
        colors = sweep(tick)
        totals = [sum(sum(colors[pixel]) for pixel in column) for column in COLUMNS]
        return totals.index(max(totals))

    assert brightest_column(0) == 0
    assert brightest_column(TICKS_PER_BAR // 2) == len(COLUMNS) - 1
    assert brightest_column(TICKS_PER_BAR - 1) == 0


def test_the_sweep_lights_a_whole_column_together():
    """The left two columns have a button on top of them; all of it lights."""
    colors = sweep(0)
    column = COLUMNS[0]
    assert len(column) == 3, "the leftmost column should include a button"
    assert len({colors[pixel] for pixel in column}) == 1


def test_the_right_hand_columns_are_pads_only():
    assert len(COLUMNS[2]) == 2
    assert len(COLUMNS[3]) == 2


def test_the_columns_between_them_cover_every_pixel():
    covered = [pixel for column in COLUMNS for pixel in column]
    assert sorted(covered) == list(range(PIXEL_COUNT))


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


def test_the_heartbeat_covers_the_whole_panel():
    assert len(_lit(heartbeat(0))) == PIXEL_COUNT


def test_the_heartbeat_beats_twice_a_bar():
    """On beats one and three, which is what a bar of four feels like."""

    def loud(beat):
        return sum(sum(color) for color in heartbeat(TICKS_PER_BEAT * beat))

    assert loud(0) > 0
    assert loud(1) == 0
    assert loud(2) > 0
    assert loud(3) == 0


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


# --- the timebase ---------------------------------------------------------
#
# The clock only advances while the transport is running, but the lights
# should keep moving when it is not. The obvious way to do that - derive a
# tick from the millisecond counter and the tempo - is wrong, and hardware is
# what showed it: an animation tick of 10765 became 120 the moment the
# transport started, because absolute-elapsed-time-times-tempo is a different
# number line from the clock's own tick. These pin down that it accumulates.


def test_the_timebase_follows_the_clock_while_it_runs():
    base = Timebase()
    assert base.step(1000, 120, clock_tick=500) == 500
    assert base.step(1020, 120, clock_tick=501) == 501


def test_the_timebase_keeps_counting_when_the_clock_stops():
    """Stepped the way the main loop does, not in one leap - see the stall guard."""
    base = Timebase()
    base.step(1000, 120, clock_tick=500)
    now = 1000
    for _ in range(50):  # one beat at 120 BPM, 10 ms at a time
        now += 10
        base.step(now, 120)
    assert base.step(now, 120) == 500 + TICKS_PER_BEAT


def test_stopping_the_transport_does_not_jump_the_animation():
    """The bug this class exists for."""
    base = Timebase()
    running = base.step(1000, 120, clock_tick=500)
    stopped = base.step(1001, 120)
    assert abs(stopped - running) <= 1, "the phase jumped when the clock stopped"


def test_starting_the_transport_does_not_jump_the_animation():
    base = Timebase()
    base.step(1000, 120, clock_tick=500)
    free = base.step(2000, 120)
    # The sequencer resets its tick on start, and the animation follows it -
    # but from that point on, not by leaping to some unrelated number first.
    assert base.step(2001, 120, clock_tick=0) == 0
    assert free > 500


def test_a_tempo_change_does_not_jump_the_animation():
    """Absolute time times tempo makes every past millisecond worth more."""
    base = Timebase()
    base.step(1000, 120)
    slow = base.step(2000, 120)
    fast = base.step(2001, 300)
    assert abs(fast - slow) <= 2, "changing tempo moved the whole timeline"


def test_a_faster_tempo_counts_faster():
    def run(bpm):
        base = Timebase()
        now = 0
        base.step(now, bpm)
        for _ in range(20):
            now += 50
            base.step(now, bpm)
        return base.step(now, bpm)

    assert run(240) > run(60)


def test_a_stall_does_not_lurch_the_animation():
    """A collection or a card read is not motion to be caught up on."""
    base = Timebase()
    base.step(1000, 120)
    after = base.step(1000 + 5000, 120)
    ceiling = int(MAX_STEP_MS * 120 * 24 / 60000.0) + 1
    assert after <= ceiling, after


def test_the_timebase_survives_the_millisecond_counter_wrapping():
    """ticks_ms rolls over at 2**29; a plain subtraction goes hugely negative."""
    base = Timebase()
    base.step((1 << 29) - 10, 120)
    before = base.step((1 << 29) - 5, 120)
    after = base.step(5, 120)  # wrapped
    assert after >= before
    assert after - before < TICKS_PER_BEAT


def test_a_zero_tempo_does_not_divide_by_it():
    base = Timebase()
    base.step(0, 0)
    assert base.step(1000, 0) == 0


def test_the_first_step_moves_nothing():
    """There is no elapsed time to account for yet."""
    base = Timebase()
    assert base.step(12345, 120) == 0


# --- Toaster ---------------------------------------------------------------


def test_the_toaster_is_red():
    for tick in range(TICKS_PER_BAR):
        for red, green, blue in toaster(tick):
            assert green == 0 and blue == 0, (red, green, blue)


def test_the_toaster_crosses_the_panel_and_comes_back():
    def brightest_column(tick):
        colors = toaster(tick)
        totals = [sum(sum(colors[pixel]) for pixel in column) for column in COLUMNS]
        return totals.index(max(totals))

    # Two round trips a bar: out by the quarter mark, back by the half.
    assert brightest_column(0) == 0
    assert brightest_column(TICKS_PER_BAR // 4) == len(COLUMNS) - 1
    assert brightest_column(TICKS_PER_BAR // 2) == 0


def test_the_toaster_has_a_smear_rather_than_a_hard_edge():
    """A single lit column reads as a fault; the eye needs a falloff."""
    lit = [color for color in toaster(TICKS_PER_BAR // 16) if color != OFF]
    assert len(lit) > len(COLUMNS[0]), lit


def test_the_toaster_crosses_the_buttons_too():
    seen = set()
    for tick in range(TICKS_PER_BAR):
        seen.update(_lit(toaster(tick)))
    for pixel in INDICATORS:
        assert pixel in seen


# --- Seven -----------------------------------------------------------------


def test_seven_is_full_red_on_the_beat():
    assert seven(0) == [(255, 0, 0)] * PIXEL_COUNT


def test_seven_strikes_every_beat():
    for beat in range(BEATS_PER_BAR):
        assert sum(seven(TICKS_PER_BEAT * beat)[0]) > 0


def test_seven_is_a_strobe_rather_than_a_swell():
    """Flat top then a fast decay, so it hits rather than breathes."""
    top = sum(seven(0)[0])
    still_on = sum(seven(1)[0])
    later = sum(seven(TICKS_PER_BEAT // 3)[0])
    assert still_on == top, "it started decaying immediately"
    assert later < top / 4, "it is fading like a swell"


def test_seven_lights_every_pixel_at_once():
    assert len(set(seven(0))) == 1


def test_seven_never_shows_anything_but_red():
    for tick in range(TICKS_PER_BAR):
        for _red, green, blue in seven(tick):
            assert green == 0 and blue == 0


# --- Bird ------------------------------------------------------------------


def test_bird_is_magenta():
    for tick in range(TICKS_PER_BAR):
        for red, green, blue in bird(tick):
            assert green == 0
            assert red == blue or (red, green, blue) == OFF, (red, green, blue)


def test_bird_never_goes_dark():
    """A glow underneath, with sparkles on top of it."""
    for tick in range(TICKS_PER_BAR):
        assert OFF not in bird(tick), tick


def test_bird_sparkles_are_brighter_than_the_glow():
    row = bird(0)
    levels = sorted({sum(color) for color in row})
    assert len(levels) > 1, "nothing is standing out from the glow"
    assert levels[0] < levels[-1]


def test_a_bird_sparkle_falls_back_to_the_glow():
    """Struck to full, then home over the next few sixteenths."""
    row = bird(0)
    brightest = max(range(PIXEL_COUNT), key=lambda i: sum(row[i]))
    peak = sum(row[brightest])
    later = sum(bird(TICKS_PER_SIXTEENTH * BIRD_FALL)[brightest])
    assert later < peak, "the sparkle never came down"


def test_most_of_bird_is_the_glow_at_any_moment():
    """Two struck a sixteenth against ten pixels, so it shimmers rather
    than sitting at full."""
    row = bird(0)
    quietest = min(sum(color) for color in row)
    at_rest = [color for color in row if sum(color) == quietest]
    assert len(at_rest) >= PIXEL_COUNT // 2, len(at_rest)


def test_bird_repeats_with_the_bar():
    assert bird(TICKS_PER_SIXTEENTH * 3) == bird(
        TICKS_PER_SIXTEENTH * 3 + TICKS_PER_BAR
    )


def test_bird_changes_every_sixteenth():
    assert bird(0) != bird(TICKS_PER_SIXTEENTH)


# --- the three of them in the list ----------------------------------------


def test_the_new_animations_are_selectable():
    for name in ("Toaster", "Seven", "Bird"):
        assert name in NAMES
        assert by_name(name) is not pulse, name
