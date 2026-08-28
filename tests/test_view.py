"""Tests for LED and display rendering.

engine.view imports nothing from CircuitPython, so a wrong colour is a failing
test here rather than something spotted by squinting at the badge.
"""

import pytest

from engine.clock import Clock
from engine.song import MAX_VELOCITY, STEPS_PER_PAGE, TRACK_COUNT, Song
from engine.transport import Transport
from engine.view import (
    ARMED,
    CLOCK_EXTERNAL,
    CLOCK_FLYWHEEL,
    LIVE,
    MODE_LIVE,
    MODE_SEQ,
    OFF,
    OUT_OF_PATTERN,
    PLAYHEAD,
    PLAYING,
    RECORDING,
    SEQ,
    STEP_ON,
    STOPPED,
    TRACK_PICK,
    TRACK_FLASH,
    TRACK_LOADED,
    TRACK_MUTED,
    TRACK_SELECTED,
    detail_line,
    function_indicator,
    live_pads,
    pads,
    play_indicator,
    scale,
    seq_pads,
    status_line,
    step_row,
    track_pads,
)

LOADED_ALL = [True] * TRACK_COUNT


@pytest.fixture
def song():
    return Song(length=16, division=3)


# --- velocity scaling -----------------------------------------------------


def test_a_silent_step_is_dark():
    assert scale(STEP_ON, 0) == OFF


def test_full_velocity_is_full_brightness():
    assert scale(STEP_ON, MAX_VELOCITY) == STEP_ON


def test_a_quiet_step_is_still_visible():
    """Dimming to invisibility would hide a note that is really there."""
    assert scale(STEP_ON, 1) != OFF


def test_brightness_rises_with_velocity():
    low = sum(scale(STEP_ON, 20))
    high = sum(scale(STEP_ON, 120))
    assert low < high


# --- SEQ pads -------------------------------------------------------------
#
# One light at a time. The pads used to paint the whole pattern - every
# recorded step lit and dimmed by its velocity - and on a diffused panel that
# reads as a wall of blue rather than as information. The pattern is on the
# screen instead, as the `*...o...` row, which is where something you read
# rather than glance at belongs.


def test_a_page_shows_eight_pads(song):
    assert len(seq_pads(song, 0, 0)) == STEPS_PER_PAGE


def test_only_the_playhead_is_lit(song):
    song.set_step(0, 2, 100)
    song.set_step(0, 5, 100)
    row = seq_pads(song, 0, 0, playhead=5)
    assert row[5] == PLAYHEAD
    assert [color for index, color in enumerate(row) if index != 5] == [OFF] * 7


def test_a_recorded_step_is_not_lit_on_its_own(song):
    """It is on the screen. Painting it here is what made the panel unreadable."""
    song.set_step(0, 2, 100)
    assert seq_pads(song, 0, 0)[2] == OFF


def test_the_playhead_shows_on_an_empty_step_too(song):
    assert seq_pads(song, 0, 0, playhead=5)[5] == PLAYHEAD


def test_nothing_is_lit_when_the_pattern_is_not_playing(song):
    """No playhead means no position to show."""
    song.set_step(0, 2, 100)
    assert seq_pads(song, 0, 0) == [OFF] * STEPS_PER_PAGE


def test_the_playhead_only_lights_on_the_page_it_is_on(song):
    """Step 9 is on page two, so page one shows nothing."""
    assert seq_pads(song, 0, 0, playhead=9) == [OFF] * STEPS_PER_PAGE
    assert seq_pads(song, 0, 1, playhead=9)[1] == PLAYHEAD


def test_steps_past_the_loop_point_are_dark_like_the_rest(song):
    """They were marked; with one light at a time there is nothing to mark."""
    song.set_length(4)
    assert seq_pads(song, 0, 0) == [OFF] * STEPS_PER_PAGE


# --- the track picker -----------------------------------------------------
#
# Function plus a pad chooses a track, so Function alone shows which one is
# chosen. Eight lit answers is not an answer to "which one".


def test_holding_function_shows_the_selected_track():
    row = track_pads(3)
    assert row[3] == TRACK_PICK
    assert [color for index, color in enumerate(row) if index != 3] == [OFF] * 7


def test_the_track_picker_covers_every_track():
    for track in range(TRACK_COUNT):
        assert track_pads(track)[track] == TRACK_PICK


def test_the_track_picker_is_one_pad_per_track():
    assert len(track_pads(0)) == TRACK_COUNT


# --- LIVE pads ------------------------------------------------------------


def test_a_track_with_a_sample_is_lit(song):
    assert live_pads(song, LOADED_ALL)[0] == TRACK_LOADED


def test_a_track_with_no_sample_is_dark(song):
    loaded = [False] * TRACK_COUNT
    assert live_pads(song, loaded)[0] == OFF


def test_the_selected_track_stands_out(song):
    assert live_pads(song, LOADED_ALL, selected=3)[3] == TRACK_SELECTED


def test_a_struck_pad_flashes(song):
    assert live_pads(song, LOADED_ALL, flashing={2})[2] == TRACK_FLASH


def test_a_flash_beats_selection(song):
    """Feedback for the hit you just played matters more than the cursor."""
    assert live_pads(song, LOADED_ALL, selected=2, flashing={2})[2] == TRACK_FLASH


def test_a_muted_track_reads_as_muted(song):
    song.toggle_mute(4)
    assert live_pads(song, LOADED_ALL)[4] == TRACK_MUTED


# --- dispatch -------------------------------------------------------------


def test_pads_dispatches_on_mode(song):
    assert pads(song, SEQ, LOADED_ALL, track=0, page=0, playhead=0)[0] == PLAYHEAD
    assert pads(song, LIVE, LOADED_ALL, track=0)[0] == TRACK_SELECTED


def test_holding_function_overrides_the_sequencer_view(song):
    row = pads(song, SEQ, LOADED_ALL, track=5, page=0, playhead=0, function_held=True)
    assert row[5] == TRACK_PICK
    assert row[0] == OFF, "the playhead was still lit under the picker"


def test_holding_function_overrides_the_live_view(song):
    row = pads(song, LIVE, LOADED_ALL, track=5, function_held=True)
    assert row[5] == TRACK_PICK
    assert row[0] == OFF


# --- indicators -----------------------------------------------------------


def test_a_stopped_transport_is_dark():
    assert play_indicator(Transport()) == STOPPED


def test_a_playing_transport_is_lit():
    t = Transport()
    t.toggle_play()
    assert play_indicator(t) == PLAYING


def test_recording_is_distinct_from_playing():
    t = Transport()
    t.toggle_play()
    t.toggle_record()
    assert play_indicator(t) == RECORDING


def test_armed_blinks_rather_than_sitting_solid():
    """Waiting for a pad hit has to look different from simply recording."""
    t = Transport()
    t.toggle_record()
    assert play_indicator(t, blink=True) == ARMED
    assert play_indicator(t, blink=False) == OFF


def test_the_mode_shows_on_the_function_pixel():
    assert function_indicator(LIVE) == MODE_LIVE
    assert function_indicator(SEQ) == MODE_SEQ


def test_an_external_clock_overrides_the_mode_colour():
    clock = Clock()
    clock.start(0)
    clock.external_pulse(100)
    assert function_indicator(LIVE, clock) == CLOCK_EXTERNAL


def test_an_internal_clock_leaves_the_mode_showing():
    clock = Clock()
    clock.start(0)
    assert function_indicator(SEQ, clock) == MODE_SEQ


# --- display --------------------------------------------------------------


def test_seq_status_names_track_and_page(song):
    line = status_line(song, SEQ, 2, 1, Transport(), Clock())
    assert "T3" in line and "P2" in line


def test_live_status_names_the_mode(song):
    assert "LIVE" in status_line(song, LIVE, 0, 0, Transport(), Clock())


def test_recording_is_flagged_on_screen(song):
    t = Transport()
    t.toggle_play()
    t.toggle_record()
    assert "REC" in status_line(song, LIVE, 0, 0, t, Clock())


def test_armed_is_flagged_differently_from_recording(song):
    t = Transport()
    t.toggle_record()
    line = status_line(song, LIVE, 0, 0, t, Clock())
    assert "ARM" in line and "REC" not in line


def test_an_external_clock_is_flagged(song):
    clock = Clock()
    clock.start(0)
    clock.external_pulse(100)
    assert "EXT" in status_line(song, LIVE, 0, 0, Transport(), clock)


def test_status_fits_the_display(song):
    """terminalio on a 128px panel is about 21 characters."""
    t = Transport()
    t.toggle_play()
    t.toggle_record()
    clock = Clock()
    clock.start(0)
    clock.external_pulse(100)
    song.set_length(64)
    line = status_line(song, SEQ, 7, 7, t, clock)
    assert len(line) <= 21, line


def test_detail_line_carries_tempo_division_and_length(song):
    clock = Clock(bpm=137)
    line = detail_line(song, clock)
    assert "137" in line and "1/16" in line and "16" in line


def test_detail_line_fits_the_display(song):
    song.set_length(64)
    assert len(detail_line(song, Clock(bpm=300))) <= 21


# --- step row -------------------------------------------------------------


def test_step_row_draws_notes_and_gaps(song):
    song.set_step(0, 0, 100)
    song.set_step(0, 4, 100)
    assert step_row(song, 0, 0) == "*...*..."


def test_step_row_marks_the_playhead(song):
    song.set_step(0, 0, 100)
    assert step_row(song, 0, 0, playhead=2) == "*.o....."


def test_step_row_blanks_past_the_loop_point(song):
    song.set_length(4)
    assert step_row(song, 0, 0) == "....    "


def test_step_row_is_one_page_wide(song):
    assert len(step_row(song, 0, 0)) == STEPS_PER_PAGE


def test_a_flywheeling_clock_blinks_rather_than_sitting_solid():
    """An external clock that stopped sending pulses must look different.

    This needs `now`, because flywheeling is a question about elapsed time.
    Reading it as an attribute silently returns False forever and the
    indicator never lights.
    """
    clock = Clock(sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    late = 1000 + 5000  # long past the flywheel threshold
    assert clock.is_flywheeling(late) is True
    assert function_indicator(LIVE, clock, blink=True, now=late) == CLOCK_FLYWHEEL
    assert function_indicator(LIVE, clock, blink=False, now=late) == OFF


def test_a_live_external_clock_stays_solid():
    clock = Clock(sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    assert function_indicator(LIVE, clock, blink=True, now=1010) == CLOCK_EXTERNAL


def test_without_a_time_an_external_clock_still_reads_as_external():
    """Callers that cannot supply `now` get the solid colour, not a wrong blink."""
    clock = Clock(sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    assert function_indicator(LIVE, clock, blink=True) == CLOCK_EXTERNAL
