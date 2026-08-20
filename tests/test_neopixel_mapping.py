"""Tests for which pixel sits at which key.

The layout was established by lighting each pixel on the badge one at a time
and writing down what was underneath it. The boards cannot settle it: the
LEDs are on the back copper of the front panel and the switches on the front
copper of the main board, so whether the two agree about left and right
depends on how the panel is mounted, which no file in the repository
records. Two earlier tables were derived from the geometry and both were
wrong, which is why these tests are written from the observation rather than
from the CAD.

Observed, walking the strip from one end:

    pixel 0     Function
    pixel 1     Play
    pixels 2-5  upper pad row, right to left   (pads 4, 3, 2, 1)
    pixels 6-9  lower pad row, left to right   (pads 5, 6, 7, 8)

The matrix numbers keys the other way - both pad rows left to right, then
Play, then Function - so the two orders disagree about the upper row and
about which end the buttons live at.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
from engine.controls import FUNCTION, PAD_FIRST, PAD_LAST, PLAY
from utils import neoindex

PIXEL_COUNT = 10

# key number -> the pixel physically at that key, as observed on the badge.
UPPER_ROW = {0: 5, 1: 4, 2: 3, 3: 2}  # left to right, pixels descending
LOWER_ROW = {4: 6, 5: 7, 6: 8, 7: 9}  # left to right, pixels ascending
BUTTONS = {PLAY: 1, FUNCTION: 0}
OBSERVED = {}
for _part in (UPPER_ROW, LOWER_ROW, BUTTONS):
    OBSERVED.update(_part)


@pytest.mark.parametrize("key,pixel", sorted(OBSERVED.items()))
def test_each_key_lights_the_pixel_beside_it(key, pixel):
    assert neoindex(key) == pixel


def test_the_buttons_are_at_the_start_of_the_strip():
    """Function first, then Play, then the pads.

    An earlier mapping put them at the far end, on pixels 8 and 9, which are
    pads - so pressing Play lit a drum pad and neither button LED ever came
    on.
    """
    assert neoindex(FUNCTION) == 0
    assert neoindex(PLAY) == 1


def test_no_pad_uses_a_button_pixel():
    for key in range(PAD_FIRST, PAD_LAST + 1):
        assert neoindex(key) not in (0, 1)


def test_the_upper_row_runs_opposite_to_the_keys():
    """Keys go left to right along the row; the strip comes back the other way."""
    pixels = [neoindex(key) for key in range(0, 4)]
    assert pixels == sorted(pixels, reverse=True)


def test_the_lower_row_runs_with_the_keys():
    pixels = [neoindex(key) for key in range(4, 8)]
    assert pixels == sorted(pixels)


def test_the_two_rows_meet_at_the_left():
    """The strip doubles back at the left-hand end of the upper row.

    Pad 1 and pad 5 are the leftmost of their rows, so their pixels are
    consecutive - that turn is the whole reason the upper row is reversed.
    """
    assert neoindex(4) - neoindex(0) == 1


def test_every_key_has_its_own_pixel():
    used = [neoindex(key) for key in range(len(OBSERVED))]
    assert sorted(used) == list(range(PIXEL_COUNT))


def test_no_key_addresses_a_pixel_that_is_not_there():
    """There are ten LEDs. Writing past them raised IndexError and dropped
    the badge to the REPL, which is how the first version of this was found.
    """
    for key in range(PAD_FIRST, PAD_LAST + 1):
        assert 0 <= neoindex(key) < PIXEL_COUNT
    for key in (PLAY, FUNCTION):
        assert 0 <= neoindex(key) < PIXEL_COUNT


def test_an_unknown_key_is_still_safe():
    """The encoder buttons are keys 10 and 11 and have no pixel of their own."""
    for key in (10, 11, 99):
        assert 0 <= neoindex(key) < PIXEL_COUNT
