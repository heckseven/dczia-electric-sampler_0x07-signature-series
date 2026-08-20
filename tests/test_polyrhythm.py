"""Tests for tracks of different lengths running against each other.

A sixteen-step kick under a twelve-step hat repeats every forty-eight steps
without either pattern being written out that long. That drift is the point,
so what these check is that the tracks really do go out of phase and come
back - not merely that the lengths can be set.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
from engine import quantize
from engine.song import MAX_STEPS, MIN_LENGTH, TRACK_COUNT, Song


@pytest.fixture
def song():
    """Two tracks that disagree: sixteen against twelve."""
    song = Song(length=16, division=3)
    song.set_track_length(0, 16)
    song.set_track_length(1, 12)
    for step in range(16):
        song.set_step(0, step, 100)
    for step in range(12):
        song.set_step(1, step, 100)
    return song


def steps_over(song, ticks, track):
    """Which step of a track fires on each tick, in order."""
    fired = []
    for tick in range(ticks):
        for hit_track, step, _velocity in quantize.hits_due(song, tick, 1.0):
            if hit_track == track:
                fired.append(step)
    return fired


# --- the lengths themselves -----------------------------------------------


def test_each_track_keeps_its_own_length(song):
    assert song.track_length(0) == 16
    assert song.track_length(1) == 12


def test_the_global_setting_moves_every_track(song):
    song.set_length(8)
    assert [song.track_length(t) for t in range(TRACK_COUNT)] == [8] * TRACK_COUNT


def test_lengths_are_clamped_like_anything_else(song):
    song.set_track_length(0, 999)
    assert song.track_length(0) == MAX_STEPS
    song.set_track_length(0, 0)
    assert song.track_length(0) == MIN_LENGTH


def test_the_song_length_is_the_longest_track(song):
    """Nothing else is meaningful once they differ."""
    assert song.length == 16
    song.set_track_length(3, 40)
    assert song.length == 40


def test_a_song_knows_when_its_tracks_disagree(song):
    assert song.uniform_length is False
    song.set_length(16)
    assert song.uniform_length is True


# --- the drift ------------------------------------------------------------


def test_a_short_track_wraps_before_a_long_one(song):
    """The whole point: at the same tick they are at different steps."""
    ticks = song.ticks_per_step
    long_track = steps_over(song, 16 * ticks, 0)
    short_track = steps_over(song, 16 * ticks, 1)
    assert long_track == list(range(16))
    # Twelve steps, then it starts again while the other is still going.
    assert short_track == list(range(12)) + list(range(4))


def test_the_two_realign_after_their_common_multiple(song):
    """Sixteen and twelve come back together after forty-eight steps."""
    ticks = song.ticks_per_step
    span = 48 * ticks
    long_track = steps_over(song, span, 0)
    short_track = steps_over(song, span, 1)
    assert long_track[0] == short_track[0] == 0
    assert long_track[-1] == 15 and short_track[-1] == 11
    # Both start their bar again on the very next tick of the cycle.
    assert quantize.hits_due(song, span, 1.0) == [(0, 0, 100), (1, 0, 100)]


def test_every_step_of_a_short_track_still_fires(song):
    ticks = song.ticks_per_step
    fired = set(steps_over(song, 12 * ticks, 1))
    assert fired == set(range(12))


def test_a_step_past_a_track_length_never_fires(song):
    """Steps beyond the loop point exist in the buffer but must stay silent."""
    song.set_step(1, 13, 120)
    ticks = song.ticks_per_step
    assert 13 not in steps_over(song, 48 * ticks, 1)


def test_shortening_a_track_silences_what_falls_outside(song):
    ticks = song.ticks_per_step
    assert 15 in steps_over(song, 16 * ticks, 0)
    song.set_track_length(0, 8)
    assert 15 not in steps_over(song, 16 * ticks, 0)


def test_lengthening_a_track_brings_its_steps_back(song):
    song.set_track_length(0, 8)
    song.set_track_length(0, 16)
    ticks = song.ticks_per_step
    assert 15 in steps_over(song, 16 * ticks, 0)


def test_tracks_of_the_same_length_stay_together(song):
    """The old behaviour has to survive, since it is still the default."""
    song.set_length(16)
    for step in range(16):
        song.set_step(1, step, 100)  # the fixture only filled twelve
    ticks = song.ticks_per_step
    assert steps_over(song, 32 * ticks, 0) == steps_over(song, 32 * ticks, 1)


# --- what the player sees -------------------------------------------------


def test_pages_follow_the_track_being_looked_at(song):
    assert song.page_count_for(0) == 2  # sixteen steps, eight to a page
    song.set_track_length(2, 40)
    assert song.page_count_for(2) == 5


def test_a_one_step_track_still_has_a_page(song):
    song.set_track_length(4, 1)
    assert song.page_count_for(4) == 1


# --- keeping it ------------------------------------------------------------


def test_lengths_survive_a_save_and_load(song):
    back = Song.from_dict(song.to_dict())
    assert back.track_length(0) == 16
    assert back.track_length(1) == 12


def test_a_song_saved_before_per_track_lengths_still_loads():
    """An older file carries one length and no per-track list."""
    data = Song(length=16, division=3).to_dict()
    del data["lengths"]
    back = Song.from_dict(data)
    assert back.uniform_length
    assert back.track_length(0) == 16


def test_a_corrupt_length_list_does_not_raise():
    data = Song(length=16, division=3).to_dict()
    data["lengths"] = ["long", None, 999]
    back = Song.from_dict(data)
    for track in range(TRACK_COUNT):
        assert MIN_LENGTH <= back.track_length(track) <= MAX_STEPS
