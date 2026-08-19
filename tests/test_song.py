"""Tests for the pattern data model.

engine.song imports nothing from CircuitPython, so these run against the real
module with no stubs involved.
"""

import pytest

from engine.song import (
    DEFAULT_VELOCITY,
    DIVISIONS,
    MAX_STEPS,
    MAX_VELOCITY,
    OFF,
    OFFSET_BIAS,
    STEPS_PER_PAGE,
    TRACK_COUNT,
    Song,
)


@pytest.fixture
def song():
    return Song()


# --- storage shape --------------------------------------------------------


def test_pattern_is_stored_as_bytearrays():
    """Lists of [on, velocity] pairs would cost tens of KB and fragment the heap."""
    song = Song()
    assert all(isinstance(row, bytearray) for row in song.steps)
    assert all(isinstance(row, bytearray) for row in song.offsets)


def test_whole_pattern_costs_about_a_kilobyte():
    song = Song()
    total = sum(len(r) for r in song.steps) + sum(len(r) for r in song.offsets)
    assert total == 2 * TRACK_COUNT * MAX_STEPS == 1024


def test_a_new_song_is_silent_and_on_the_grid():
    song = Song()
    for track in range(TRACK_COUNT):
        assert set(song.steps[track]) == {OFF}
        assert set(song.offsets[track]) == {OFFSET_BIAS}


# --- steps ----------------------------------------------------------------


def test_setting_a_step_records_its_velocity(song):
    song.set_step(0, 4, 90)
    assert song.is_on(0, 4)
    assert song.velocity(0, 4) == 90


def test_clearing_a_step_silences_it_and_resets_timing(song):
    song.set_step(0, 4, 90, offset=2)
    song.clear_step(0, 4)
    assert not song.is_on(0, 4)
    assert song.offset(0, 4) == 0


def test_tapping_a_lit_pad_clears_and_an_unlit_pad_sets(song):
    assert song.toggle_step(1, 2) is True
    assert song.velocity(1, 2) == DEFAULT_VELOCITY
    assert song.toggle_step(1, 2) is False
    assert not song.is_on(1, 2)


def test_velocity_is_clamped_to_the_byte_range(song):
    song.set_step(0, 0, 9999)
    assert song.velocity(0, 0) == MAX_VELOCITY
    song.set_step(0, 1, -5)
    assert song.velocity(0, 1) == OFF


def test_velocity_edit_leaves_an_empty_step_empty(song):
    assert song.set_velocity(0, 5, 120) == OFF
    assert not song.is_on(0, 5)


def test_velocity_edit_changes_a_live_step(song):
    song.set_step(0, 5)
    song.set_velocity(0, 5, 40)
    assert song.velocity(0, 5) == 40


def test_tracks_are_independent(song):
    song.set_step(0, 3)
    assert not song.is_on(1, 3)


def test_clear_track_leaves_other_tracks_alone(song):
    song.set_step(0, 1)
    song.set_step(1, 1)
    song.clear_track(0)
    assert not song.is_on(0, 1)
    assert song.is_on(1, 1)


def test_is_empty_only_considers_steps_within_the_length(song):
    song.set_length(8)
    song.set_step(0, 30)  # beyond the loop point
    assert song.is_empty()
    song.set_step(0, 3)
    assert not song.is_empty()


# --- pattern shape --------------------------------------------------------


def test_length_is_clamped_to_the_buffer(song):
    assert song.set_length(999) == MAX_STEPS
    assert song.set_length(0) == 1


@pytest.mark.parametrize(
    "index,name,ticks", [(i, d[0], d[1]) for i, d in enumerate(DIVISIONS)]
)
def test_every_division_divides_24_ppqn_evenly(index, name, ticks):
    song = Song(division=index)
    assert song.division_name == name
    assert song.ticks_per_step == ticks
    # Every division divides the 24 PPQN quarter exactly, so no grid drifts.
    assert 24 % ticks == 0


def test_page_count_follows_length(song):
    song.set_length(8)
    assert song.page_count == 1
    song.set_length(9)
    assert song.page_count == 2
    song.set_length(MAX_STEPS)
    assert song.page_count == MAX_STEPS // STEPS_PER_PAGE == 8


def test_bpm_is_clamped(song):
    assert song.set_bpm(5) == 20
    assert song.set_bpm(9999) == 300


# --- micro-timing ---------------------------------------------------------


def test_offset_is_clamped_to_half_a_step(song):
    song.set_division(3)  # 1/16 -> 6 ticks per step
    assert song.max_offset == 3
    assert song.set_offset(0, 0, 99) == 3
    assert song.set_offset(0, 1, -99) == -3


def test_offset_clamping_keeps_the_nearest_pad_correct(song):
    """A hit can never drift closer to a neighbour than to its own step."""
    for index in range(len(DIVISIONS)):
        song.set_division(index)
        limit = song.max_offset
        song.set_offset(0, 5, 1000)
        assert abs(song.offset(0, 5)) <= song.ticks_per_step / 2.0
        assert song.offset(0, 5) == limit


def test_offsets_are_reclamped_when_the_grid_gets_finer(song):
    song.set_division(0)  # 1/4 -> 24 ticks, offsets to +/-12
    song.set_step(0, 0, 100, offset=12)
    assert song.offset(0, 0) == 12
    song.set_division(5)  # 1/32 -> 3 ticks, offsets to +/-1
    assert song.offset(0, 0) == 1


def test_offsets_survive_a_coarser_grid_unchanged(song):
    song.set_division(3)
    song.set_step(0, 0, 100, offset=2)
    song.set_division(0)
    assert song.offset(0, 0) == 2


# --- tracks and kit -------------------------------------------------------


def test_mute_toggles(song):
    assert song.toggle_mute(2) is True
    assert song.muted[2] is True
    assert song.toggle_mute(2) is False


def test_sample_assignment(song):
    song.set_sample(0, "/sd/kits/909/kick.wav")
    assert song.kit[0] == "/sd/kits/909/kick.wav"
    assert song.kit[1] is None


# --- persistence ----------------------------------------------------------


def test_round_trip_preserves_everything(song):
    song.set_length(32)
    song.set_division(1)
    song.set_bpm(174)
    song.kit_name = "909"
    song.set_sample(0, "kick.wav")
    song.set_step(0, 0, 120, offset=2)
    song.set_step(3, 17, 40)
    song.toggle_mute(5)

    restored = Song.from_dict(song.to_dict())

    assert restored.length == 32
    assert restored.division == 1
    assert restored.bpm == 174
    assert restored.kit_name == "909"
    assert restored.kit[0] == "kick.wav"
    assert restored.velocity(0, 0) == 120
    assert restored.offset(0, 0) == 2
    assert restored.velocity(3, 17) == 40
    assert restored.muted[5] is True


def test_serialised_rows_are_bytes_not_int_lists(song):
    """msgpack encodes bytes compactly; a list of 64 ints would not."""
    data = song.to_dict()
    assert all(isinstance(row, bytes) for row in data["steps"])
    assert all(isinstance(row, bytes) for row in data["offsets"])


def test_from_dict_tolerates_a_truncated_payload():
    """A short or partial file must not raise on load."""
    restored = Song.from_dict({"length": 4, "steps": [b"\x64\x00"], "offsets": []})
    assert restored.length == 4
    assert restored.velocity(0, 0) == 100
    assert restored.velocity(0, 1) == OFF
    assert restored.velocity(7, 0) == OFF


def test_from_dict_on_an_empty_payload_gives_a_usable_song():
    restored = Song.from_dict({})
    assert restored.is_empty()
    assert restored.length >= 1
