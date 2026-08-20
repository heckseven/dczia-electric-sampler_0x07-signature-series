"""Tests for turning knob speed into step size.

A rotary encoder reports detents, not speed, so the only clue to how fast a
hand is moving is how close together the detents arrive. Reading that back
is what makes a knob feel like a knob: creep it for a small correction, spin
it to cross the range, without a modifier or a second control.
"""

import pytest

from engine.util import FAST_MS, MAX_FACTOR, SLOW_MS, accelerated


def test_a_still_knob_is_not_accelerated():
    """No previous movement to measure against."""
    assert accelerated(1, None) == 1
    assert accelerated(-3, None) == -3


def test_an_unhurried_turn_is_not_accelerated():
    assert accelerated(1, SLOW_MS) == 1
    assert accelerated(1, SLOW_MS * 4) == 1


def test_a_fast_turn_is_accelerated_fully():
    assert accelerated(1, FAST_MS) == MAX_FACTOR
    assert accelerated(1, 0) == MAX_FACTOR


def test_acceleration_rises_as_the_gap_shrinks():
    gaps = [SLOW_MS, SLOW_MS // 2, SLOW_MS // 4, FAST_MS]
    scaled = [accelerated(1, gap) for gap in gaps]
    assert scaled == sorted(scaled), scaled
    assert scaled[0] < scaled[-1]


def test_direction_is_preserved():
    for gap in (None, SLOW_MS, SLOW_MS // 2, FAST_MS):
        assert accelerated(-1, gap) < 0
        assert accelerated(1, gap) > 0


def test_a_movement_never_rounds_away_to_nothing():
    """The factor is at least one, but this guards the arithmetic anyway.

    A detent the player felt has to move the value. Rounding one away would
    make the knob feel broken at exactly the speed people use for fine
    adjustment.
    """
    for gap in range(0, SLOW_MS + 50, 5):
        assert abs(accelerated(1, gap)) >= 1
        assert abs(accelerated(-1, gap)) >= 1


def test_no_movement_stays_no_movement():
    assert accelerated(0, 5) == 0
    assert accelerated(0, None) == 0


def test_several_detents_at_speed_compound():
    """A fast spin arrives as several detents in one pass, and both count."""
    assert abs(accelerated(4, FAST_MS)) > abs(accelerated(1, FAST_MS))


@pytest.mark.parametrize("gap", [0, 1, FAST_MS, 100, SLOW_MS, 10_000])
def test_the_factor_never_exceeds_the_maximum(gap):
    """Otherwise a fast spin could jump the whole range unpredictably."""
    assert abs(accelerated(1, gap)) <= MAX_FACTOR
