"""Tests for play / stop / record state."""

import pytest

from engine.transport import ARMED, LIVE, OFF, ON, SEQ, Transport


@pytest.fixture
def transport():
    return Transport()


# --- play and stop --------------------------------------------------------


def test_starts_stopped(transport):
    assert transport.stopped
    assert not transport.recording


def test_play_starts_and_stops(transport):
    transport.toggle_play()
    assert transport.playing
    transport.toggle_play()
    assert transport.stopped


def test_starting_resets_the_playhead(transport):
    assert transport.toggle_play() is True


def test_stopping_does_not_ask_for_a_reset(transport):
    transport.toggle_play()
    assert transport.toggle_play() is False


def test_starting_an_already_running_transport_is_a_no_op(transport):
    transport.start()
    assert transport.start() is False
    assert transport.playing


# --- arming while stopped -------------------------------------------------


def test_arming_while_stopped_waits_for_a_pad(transport):
    transport.toggle_record()
    assert transport.armed
    assert transport.stopped, "arming must not start the pattern by itself"


def test_the_first_pad_hit_starts_the_take(transport):
    transport.toggle_record()
    assert transport.pad_hit(LIVE) is True
    assert transport.playing
    assert transport.recording


def test_that_first_hit_is_itself_recorded(transport):
    """Punching in on the hit is the point: it must not be swallowed."""
    transport.toggle_record()
    transport.pad_hit(LIVE)
    assert transport.should_capture(LIVE)


def test_a_pad_in_seq_mode_does_not_punch_in(transport):
    """SEQ pads edit steps; there is nothing to record with."""
    transport.toggle_record()
    assert transport.pad_hit(SEQ) is False
    assert transport.stopped
    assert transport.armed, "still armed, just not started by a step edit"


def test_arming_twice_while_stopped_cancels(transport):
    transport.toggle_record()
    assert transport.armed
    transport.toggle_record()
    assert transport.record == OFF
    assert transport.stopped


def test_play_while_armed_starts_and_records(transport):
    """In SEQ there is no pad to punch in with, so Play must still work."""
    transport.toggle_record()
    transport.toggle_play()
    assert transport.playing
    assert transport.recording


# --- arming while running -------------------------------------------------


def test_arming_while_running_latches_immediately(transport):
    transport.toggle_play()
    transport.toggle_record()
    assert transport.recording
    assert transport.playing


def test_arming_twice_while_running_turns_recording_off(transport):
    transport.toggle_play()
    transport.toggle_record()
    transport.toggle_record()
    assert transport.record == OFF
    assert transport.playing, "disarming must not stop the pattern"


def test_recording_survives_looping(transport):
    """Recording latches; it does not expire after one pass."""
    transport.toggle_play()
    transport.toggle_record()
    for _ in range(10):
        assert transport.recording


# --- stopping -------------------------------------------------------------


def test_stopping_disarms(transport):
    """Otherwise the next Play would silently start capturing."""
    transport.toggle_play()
    transport.toggle_record()
    transport.toggle_play()
    assert transport.stopped
    assert transport.record == OFF


def test_stopping_while_armed_clears_the_arm(transport):
    transport.toggle_record()
    transport.stop()
    assert transport.record == OFF


def test_restarting_after_a_stop_does_not_record(transport):
    transport.toggle_play()
    transport.toggle_record()
    transport.toggle_play()  # stop
    transport.toggle_play()  # start again
    assert transport.playing
    assert not transport.recording


# --- capture gating -------------------------------------------------------


def test_nothing_is_captured_when_not_recording(transport):
    transport.toggle_play()
    assert transport.should_capture(LIVE) is False


def test_nothing_is_captured_while_stopped(transport):
    transport.toggle_record()
    assert transport.should_capture(LIVE) is False


def test_nothing_is_captured_in_seq_mode(transport):
    transport.toggle_play()
    transport.toggle_record()
    assert transport.should_capture(SEQ) is False
    assert transport.should_capture(LIVE) is True


def test_a_pad_hit_while_already_running_does_not_restart(transport):
    transport.toggle_play()
    transport.toggle_record()
    assert transport.pad_hit(LIVE) is False
    assert transport.playing


# --- full sequences -------------------------------------------------------


def test_a_complete_punch_in_take(transport):
    """Arm from stopped, hit a pad, record a few bars, disarm, keep playing."""
    transport.toggle_record()
    assert transport.armed and transport.stopped

    assert transport.pad_hit(LIVE) is True
    assert transport.playing and transport.recording

    transport.toggle_record()
    assert not transport.recording
    assert transport.playing

    transport.toggle_play()
    assert transport.stopped
    assert transport.record == OFF


def test_overdub_onto_a_running_pattern(transport):
    transport.toggle_play()
    assert transport.playing and not transport.recording
    transport.toggle_record()
    assert transport.should_capture(LIVE)
    transport.toggle_record()
    assert not transport.should_capture(LIVE)
    assert transport.playing
