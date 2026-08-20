"""Tests for the control surface: modifiers, chords and taps.

The whole point of the consumed-flag scheme is that a modifier press is
ambiguous until release, so these tests drive full press/release sequences
rather than single events.
"""

import pytest

from engine.controls import (
    ARM_RECORD,
    SETTINGS,
    CLEAR_TRACK,
    ERASE,
    FUNCTION,
    LIVE,
    MUTE,
    PAD,
    PAD_RELEASE,
    PAGE,
    PLAY,
    SELECT,
    SELECT_TRACK,
    SEQ,
    TOGGLE_MODE,
    TOGGLE_TRANSPORT,
    VOLUME,
    Controls,
)


@pytest.fixture
def controls():
    return Controls()


def actions(controls, *events):
    """Run (key, pressed) events and collect every action produced."""
    out = []
    for key, pressed in events:
        out.extend(controls.handle(key, pressed))
    return out


# --- plain pads -----------------------------------------------------------


def test_a_pad_press_triggers_it(controls):
    assert controls.press(3) == [(PAD, 3)]


def test_a_pad_release_is_reported(controls):
    controls.press(3)
    assert controls.release(3) == [(PAD_RELEASE, 3)]


def test_several_pads_can_be_held_at_once(controls):
    controls.press(1)
    controls.press(5)
    assert controls.held_pads == [1, 5]
    controls.release(1)
    assert controls.held_pads == [5]


# --- Function as tap versus modifier --------------------------------------


def test_function_tapped_toggles_mode(controls):
    """Nothing pressed in between, so it was a tap."""
    assert actions(controls, (FUNCTION, True), (FUNCTION, False)) == [
        (TOGGLE_MODE, None)
    ]


def test_function_plus_pad_selects_a_track(controls):
    result = actions(
        controls, (FUNCTION, True), (2, True), (2, False), (FUNCTION, False)
    )
    assert (SELECT_TRACK, 2) in result


def test_a_consumed_function_does_not_also_toggle_mode(controls):
    """The ambiguity this whole scheme exists to resolve."""
    result = actions(
        controls, (FUNCTION, True), (2, True), (2, False), (FUNCTION, False)
    )
    assert (TOGGLE_MODE, None) not in result


def test_function_plus_pad_does_not_trigger_the_pad(controls):
    result = actions(controls, (FUNCTION, True), (2, True))
    assert (PAD, 2) not in result


def test_function_still_taps_after_an_earlier_chord(controls):
    """The flag must reset on each press, or a tap stops working."""
    actions(controls, (FUNCTION, True), (2, True), (2, False), (FUNCTION, False))
    assert actions(controls, (FUNCTION, True), (FUNCTION, False)) == [
        (TOGGLE_MODE, None)
    ]


# --- Play as tap versus modifier ------------------------------------------


def test_play_tapped_toggles_the_transport(controls):
    assert actions(controls, (PLAY, True), (PLAY, False)) == [(TOGGLE_TRANSPORT, None)]


def test_play_plus_pad_pages_in_seq(controls):
    controls.set_mode(SEQ)
    result = actions(controls, (PLAY, True), (5, True))
    assert result == [(PAGE, 5)]


def test_play_plus_pad_erases_in_live(controls):
    controls.set_mode(LIVE)
    result = actions(controls, (PLAY, True), (5, True))
    assert result == [(ERASE, 5)]


def test_a_consumed_play_does_not_toggle_the_transport(controls):
    """Paging must not also start or stop the beat."""
    controls.set_mode(SEQ)
    result = actions(controls, (PLAY, True), (5, True), (5, False), (PLAY, False))
    assert (TOGGLE_TRANSPORT, None) not in result


# --- the two-modifier chord -----------------------------------------------


def test_function_then_play_arms_record(controls):
    result = actions(controls, (FUNCTION, True), (PLAY, True))
    assert result == [(ARM_RECORD, None)]


def test_play_then_function_also_arms_record(controls):
    """Order independent: whichever is pressed first, the chord is the same."""
    result = actions(controls, (PLAY, True), (FUNCTION, True))
    assert result == [(ARM_RECORD, None)]


@pytest.mark.parametrize("first,second", [(FUNCTION, PLAY), (PLAY, FUNCTION)])
def test_the_record_chord_consumes_both_keys(controls, first, second):
    """Neither key may fire its tap action when the chord is released."""
    result = actions(
        controls, (first, True), (second, True), (second, False), (first, False)
    )
    assert (TOGGLE_MODE, None) not in result
    assert (TOGGLE_TRANSPORT, None) not in result
    assert result.count((ARM_RECORD, None)) == 1


def test_arming_twice_needs_two_chords(controls):
    first = actions(
        controls, (FUNCTION, True), (PLAY, True), (PLAY, False), (FUNCTION, False)
    )
    second = actions(
        controls, (FUNCTION, True), (PLAY, True), (PLAY, False), (FUNCTION, False)
    )
    assert first.count((ARM_RECORD, None)) == 1
    assert second.count((ARM_RECORD, None)) == 1


# --- encoder buttons ------------------------------------------------------


def test_select_click_goes_back(controls):
    assert controls.press(SELECT) == [(SETTINGS, None)]


def test_volume_click_mutes(controls):
    assert controls.press(VOLUME) == [(MUTE, None)]


def test_function_plus_volume_click_clears_the_track(controls):
    result = actions(controls, (FUNCTION, True), (VOLUME, True))
    assert result == [(CLEAR_TRACK, None)]


def test_clearing_a_track_does_not_also_toggle_mode(controls):
    result = actions(controls, (FUNCTION, True), (VOLUME, True), (FUNCTION, False))
    assert (TOGGLE_MODE, None) not in result


# --- encoder context ------------------------------------------------------


def test_select_encoder_defaults_to_bpm(controls):
    assert controls.select_turn_target() == "bpm"


def test_function_puts_length_on_the_select_encoder(controls):
    controls.press(FUNCTION)
    assert controls.select_turn_target() == "length"


def test_function_puts_division_on_the_volume_encoder(controls):
    controls.press(FUNCTION)
    assert controls.volume_turn_target() == "division"


def test_play_puts_quantise_on_the_volume_encoder(controls):
    controls.press(PLAY)
    assert controls.volume_turn_target() == "quantize"


def test_holding_a_pad_puts_step_velocity_on_the_select_encoder(controls):
    controls.press(2)
    assert controls.select_turn_target() == "step_velocity"


def test_releasing_the_pad_returns_the_encoder_to_bpm(controls):
    controls.press(2)
    controls.release(2)
    assert controls.select_turn_target() == "bpm"


def test_volume_encoder_defaults_to_volume(controls):
    assert controls.volume_turn_target() == "volume"


# --- no gesture strands the surface ---------------------------------------


def test_every_key_can_be_pressed_and_released_without_error(controls):
    for key in range(12):
        controls.handle(key, True)
        controls.handle(key, False)
    assert controls.held_pads == []
    assert not controls.function_held
    assert not controls.play_held


def test_modifiers_released_out_of_order_leave_no_state(controls):
    actions(controls, (FUNCTION, True), (PLAY, True), (FUNCTION, False), (PLAY, False))
    assert not controls.function_held
    assert not controls.play_held
