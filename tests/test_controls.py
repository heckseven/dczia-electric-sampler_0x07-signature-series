"""Tests for the control surface: modifiers, chords and taps.

The whole point of the consumed-flag scheme is that a modifier press is
ambiguous until release, so these tests drive full press/release sequences
rather than single events.
"""

import pytest

from engine.controls import (
    HOLD_MS,
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


def test_function_puts_the_tracks_pitch_on_the_select_encoder(controls):
    controls.press(FUNCTION)
    assert controls.select_turn_target() == "track_pitch"


def test_function_puts_the_tracks_volume_on_the_volume_encoder(controls):
    controls.press(FUNCTION)
    assert controls.volume_turn_target() == "track_volume"


def test_play_puts_quantise_on_the_volume_encoder(controls):
    controls.press(PLAY)
    assert controls.volume_turn_target() == "quantize"


def test_holding_a_pad_in_seq_puts_step_velocity_on_the_volume_encoder():
    controls = Controls(mode=SEQ)
    controls.press(2)
    assert controls.volume_turn_target() == "step_velocity"


def test_holding_a_pad_in_live_puts_that_tracks_volume_on_the_volume_encoder():
    """A pad is a step in SEQ and a track in LIVE, so the same chord scopes
    to whichever the pad currently means."""
    controls = Controls(mode=LIVE)
    controls.press(2)
    assert controls.volume_turn_target() == "track_volume_held"


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


# --- tap versus hold -------------------------------------------------------
#
# The consumed flag handles chords. It cannot handle holding a modifier by
# itself, which is now a real gesture - Function shows a track picker, Play
# shows the pages - with nothing to consume it. Duration is the second test,
# and HOLD_MS is the whole of its definition.


def test_a_quick_tap_of_function_still_switches_mode():
    controls = Controls()
    controls.press(FUNCTION, now=1000)
    assert controls.release(FUNCTION, now=1000 + HOLD_MS - 1) == [(TOGGLE_MODE, None)]


def test_holding_function_and_letting_go_does_nothing():
    """Releasing a hold used to toggle the mode, which mid-set is a surprise."""
    controls = Controls()
    controls.press(FUNCTION, now=1000)
    assert controls.release(FUNCTION, now=1000 + HOLD_MS) == []


def test_a_quick_tap_of_play_still_moves_the_transport():
    controls = Controls()
    controls.press(PLAY, now=1000)
    assert controls.release(PLAY, now=1000 + HOLD_MS - 1) == [(TOGGLE_TRANSPORT, None)]


def test_holding_play_and_letting_go_does_not_move_the_transport():
    controls = Controls()
    controls.press(PLAY, now=1000)
    assert controls.release(PLAY, now=1000 + HOLD_MS + 500) == []


def test_the_threshold_is_one_number():
    """Raising or lowering HOLD_MS must move every gesture at once."""
    import engine.controls as module

    original = module.HOLD_MS
    try:
        module.HOLD_MS = 1000
        controls = Controls()
        controls.press(FUNCTION, now=0)
        assert controls.release(FUNCTION, now=500) == [(TOGGLE_MODE, None)]
        controls.press(PLAY, now=0)
        assert controls.release(PLAY, now=500) == [(TOGGLE_TRANSPORT, None)]
    finally:
        module.HOLD_MS = original


def test_without_a_clock_every_press_is_a_tap():
    """The chord tests are pure logic and pass no time at all."""
    controls = Controls()
    controls.press(FUNCTION)
    assert controls.release(FUNCTION) == [(TOGGLE_MODE, None)]


def test_a_consumed_modifier_stays_silent_however_briefly_it_was_held():
    controls = Controls()
    controls.press(FUNCTION, now=1000)
    controls.press(0, now=1010)
    assert controls.release(FUNCTION, now=1020) == []


def test_is_hold_says_no_before_the_threshold():
    controls = Controls()
    controls.press(FUNCTION, now=1000)
    assert controls.is_hold(FUNCTION, 1000 + HOLD_MS - 1) is False
    assert controls.is_hold(FUNCTION, 1000 + HOLD_MS) is True


def test_a_key_that_is_not_down_is_not_held():
    controls = Controls()
    assert controls.held_long(FUNCTION, 99999) is False


def test_held_long_is_what_the_legend_waits_for():
    controls = Controls()
    controls.press(PLAY, now=1000)
    assert controls.held_long(PLAY, 1100) is False
    assert controls.held_long(PLAY, 1000 + HOLD_MS) is True


def test_releasing_forgets_when_the_key_went_down():
    controls = Controls()
    controls.press(FUNCTION, now=1000)
    controls.release(FUNCTION, now=2000)
    assert controls.is_hold(FUNCTION, 3000) is False


# --- the legend ------------------------------------------------------------


def test_each_modifier_names_its_own_gestures():
    controls = Controls()
    controls.press(FUNCTION)
    assert controls.legend() == ("pad  track", "Sel  pitch", "Vol  volume")
    controls.release(FUNCTION)
    controls.press(PLAY)
    assert controls.legend() == ("pad  page", "Sel  length", "Vol  quantize")


def test_a_held_pad_names_what_it_scopes_to():
    controls = Controls(mode=SEQ)
    controls.press(0)
    assert controls.legend()[0] == "Sel  step pitch"
    controls.release(0)
    live = Controls(mode=LIVE)
    live.press(0)
    assert live.legend()[0] == "Sel  pitch"


def test_nothing_held_has_no_legend():
    assert Controls().legend() is None


def test_every_legend_line_fits_the_display():
    """terminalio on a 128px panel is about 21 characters."""
    for mode in (LIVE, SEQ):
        for key in (FUNCTION, PLAY, 0):
            controls = Controls(mode=mode)
            controls.press(key)
            for line in controls.legend():
                assert len(line) <= 21, line


# --- is anything being held, asked on every pass ---------------------------
#
# The main loop asks this twice a pass to decide whether the legend is on
# screen, so what it allocates matters as much as what it returns. See
# Controls.any_held_long for the measured numbers these exist to protect.


def test_nothing_held_is_not_a_hold():
    assert Controls().any_held_long(HOLD_MS * 10) is False


def test_a_modifier_not_yet_past_the_threshold_is_not_a_hold():
    controls = Controls()
    controls.press(FUNCTION, now=0)

    assert controls.any_held_long(HOLD_MS - 1) is False


def test_a_held_function_is_a_hold():
    controls = Controls()
    controls.press(FUNCTION, now=0)

    assert controls.any_held_long(HOLD_MS) is True


def test_a_held_play_is_a_hold():
    controls = Controls()
    controls.press(PLAY, now=0)

    assert controls.any_held_long(HOLD_MS) is True


def test_a_held_pad_is_a_hold():
    controls = Controls()
    controls.press(2, now=0)

    assert controls.any_held_long(HOLD_MS) is True


def test_a_released_modifier_is_not_a_hold():
    """The legend must come down when the finger comes off."""
    controls = Controls()
    controls.press(FUNCTION, now=0)
    controls.release(FUNCTION, now=HOLD_MS)

    assert controls.any_held_long(HOLD_MS * 2) is False


def test_it_agrees_with_asking_each_key_in_turn():
    """The behaviour it replaced, kept honest across every combination."""
    combinations = (
        (),
        (FUNCTION,),
        (PLAY,),
        (2,),
        (2, 5),
        (FUNCTION, 2),
        (PLAY, 3),
        # Both modifiers at once is reachable - it is the record chord - and
        # any_held_long short-circuits on FUNCTION before it looks at PLAY.
        (FUNCTION, PLAY),
    )
    for keys in combinations:
        for elapsed in (0, HOLD_MS - 1, HOLD_MS, HOLD_MS * 3):
            controls = Controls()
            for key in keys:
                controls.press(key, now=0)
            expected = any(
                controls.held_long(key, elapsed)
                for key in (FUNCTION, PLAY) + tuple(controls.held_pads)
            )

            assert controls.any_held_long(elapsed) is expected, (keys, elapsed)


def test_it_does_not_go_through_the_allocating_property(monkeypatch):
    """What actually pins the fix, since equivalence alone would not.

    The test above computes what it expects using the very spelling this
    replaced, so restoring that spelling would leave every test in this file
    green. held_pads builds a sorted list each time it is read; reading it
    twice a pass is the bug, whatever the answer comes out as.
    """
    controls = Controls()
    controls.press(2, now=0)

    def explode(self):
        raise AssertionError("any_held_long read held_pads")

    monkeypatch.setattr(Controls, "held_pads", property(explode))

    assert controls.any_held_long(HOLD_MS) is True


def test_a_pad_held_long_is_found_past_one_that_is_not():
    """Two pads, different ages: the scan must not stop at the first False.

    Every other case here presses its keys together, so the loop only ever
    sees pads that agree with each other.
    """
    controls = Controls()
    controls.press(2, now=0)
    controls.press(5, now=HOLD_MS)

    assert controls.held_long(5, HOLD_MS + 1) is False
    assert controls.held_long(2, HOLD_MS + 1) is True
    assert controls.any_held_long(HOLD_MS + 1) is True


def test_without_a_clock_nothing_is_held_long():
    """`now` is optional everywhere in this module - see its docstring."""
    controls = Controls()
    controls.press(FUNCTION, now=0)
    controls.press(2, now=0)

    assert controls.any_held_long() is False
