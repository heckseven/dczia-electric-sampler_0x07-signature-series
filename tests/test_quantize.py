"""Tests for quantise strength and hit scheduling."""

import pytest

from engine.quantize import (
    DEFAULT_STRENGTH,
    MAX_STRENGTH,
    MIN_STRENGTH,
    clamp_strength,
    effective_offset,
    hits_due,
    nearest_step,
    quantize_hit,
    step_fires_at,
)
from engine.song import Song

SIXTEENTH = 3  # index into DIVISIONS: 1/16, six ticks per step


@pytest.fixture
def song():
    s = Song(length=16, division=SIXTEENTH)
    return s


# --- strength -------------------------------------------------------------


def test_full_strength_snaps_to_the_grid():
    assert effective_offset(2, 1.0) == 0
    assert effective_offset(-2, 1.0) == 0


def test_zero_strength_plays_exactly_as_performed():
    assert effective_offset(2, 0.0) == 2
    assert effective_offset(-2, 0.0) == -2


def test_half_strength_pulls_halfway():
    assert effective_offset(2, 0.5) == 1
    assert effective_offset(-2, 0.5) == -1


def test_strength_is_clamped():
    assert clamp_strength(-1) == MIN_STRENGTH
    assert clamp_strength(5) == MAX_STRENGTH


def test_an_on_grid_hit_is_unaffected_by_strength():
    for strength in (0.0, 0.25, 0.5, 1.0):
        assert effective_offset(0, strength) == 0


def test_strength_is_reversible():
    """The point of applying strength at playback: nothing is overwritten."""
    stored = 2
    assert effective_offset(stored, 1.0) == 0
    assert effective_offset(stored, 0.0) == stored


def test_strength_moves_a_hit_monotonically_toward_the_grid():
    previous = None
    for strength in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        current = effective_offset(11, strength)
        if previous is not None:
            assert current <= previous
        previous = current
    assert previous == 0


def test_a_residue_below_half_a_tick_is_on_the_grid():
    """Ticks are the finest thing playable, so 0.3 of one is not a delay."""
    assert effective_offset(3, 0.9) == 0


def test_default_is_fully_quantised():
    assert DEFAULT_STRENGTH == MAX_STRENGTH


# --- nearest step ---------------------------------------------------------


def test_nearest_step_is_the_containing_grid_line():
    assert nearest_step(0, 6, 16) == 0
    assert nearest_step(2, 6, 16) == 0  # within the first half-step
    assert nearest_step(4, 6, 16) == 1  # past halfway, belongs to step 1
    assert nearest_step(6, 6, 16) == 1


def test_nearest_step_wraps_at_the_pattern_end():
    assert nearest_step(16 * 6 - 1, 6, 16) == 0


# --- scheduling -----------------------------------------------------------


def test_a_quantised_hit_fires_on_its_grid_line(song):
    song.set_step(0, 4, 100, offset=2)  # +2 is the limit at 1/16
    assert step_fires_at(song, 0, 4, 1.0) == 4 * 6


def test_an_unquantised_hit_fires_late(song):
    song.set_step(0, 4, 100, offset=2)
    assert step_fires_at(song, 0, 4, 0.0) == 4 * 6 + 2


def test_hits_due_finds_the_hit_on_its_tick(song):
    song.set_step(0, 4, 100)
    assert hits_due(song, 4 * 6, 1.0) == [(0, 4, 100)]


def test_hits_due_is_empty_off_the_beat(song):
    song.set_step(0, 4, 100)
    assert hits_due(song, 4 * 6 + 1, 1.0) == []


def test_hits_due_returns_every_track_on_a_shared_tick(song):
    song.set_step(0, 0, 100)
    song.set_step(3, 0, 80)
    song.set_step(7, 0, 60)
    due = hits_due(song, 0, 1.0)
    assert sorted(t for t, _, _ in due) == [0, 3, 7]


def test_muted_tracks_do_not_sound(song):
    song.set_step(0, 0, 100)
    song.toggle_mute(0)
    assert hits_due(song, 0, 1.0) == []
    assert hits_due(song, 0, 1.0, include_muted=True) == [(0, 0, 100)]


def test_an_offset_hit_moves_with_strength(song):
    song.set_step(0, 4, 100, offset=2)
    assert hits_due(song, 4 * 6, 1.0) == [(0, 4, 100)]  # snapped
    assert hits_due(song, 4 * 6, 0.0) == []  # not yet
    assert hits_due(song, 4 * 6 + 2, 0.0) == [(0, 4, 100)]  # played late


def test_every_hit_fires_exactly_once_per_loop(song):
    """No hit may be dropped or double-triggered at any strength."""
    song.set_step(0, 0, 100, offset=-2)
    song.set_step(1, 5, 100, offset=1)
    song.set_step(2, 15, 100, offset=2)
    total = song.length * song.ticks_per_step
    for strength in (0.0, 0.25, 0.5, 0.75, 1.0):
        fired = {}
        for tick in range(total):
            for track, step, _ in hits_due(song, tick, strength):
                fired[(track, step)] = fired.get((track, step), 0) + 1
        assert sorted(fired) == [(0, 0), (1, 5), (2, 15)], strength
        assert set(fired.values()) == {1}, strength


def test_a_hit_pushed_before_the_downbeat_lands_at_the_end_of_the_loop(song):
    """Playing ahead of beat one means the hit sounds at the end of the bar."""
    song.set_step(0, 0, 100, offset=-2)
    total = song.length * song.ticks_per_step
    assert step_fires_at(song, 0, 0, 0.0) == total - 2


# --- capturing a live hit -------------------------------------------------


def test_a_hit_on_the_beat_records_with_no_offset():
    step, offset = quantize_hit(4 * 6, 6, 16)
    assert (step, offset) == (4, 0)


def test_a_late_hit_records_a_positive_offset():
    step, offset = quantize_hit(4 * 6 + 2, 6, 16)
    assert (step, offset) == (4, 2)


def test_an_early_hit_belongs_to_the_step_it_is_nearest():
    step, offset = quantize_hit(4 * 6 - 2, 6, 16)
    assert (step, offset) == (4, -2)


def test_a_hit_just_before_the_loop_point_wraps_to_step_zero():
    """It must not be recorded as the last step, nearly a whole bar late."""
    total = 16 * 6
    step, offset = quantize_hit(total - 2, 6, 16)
    assert step == 0
    assert offset == -2


def test_captured_offsets_are_storable_by_song():
    """Capture must not produce an offset Song would silently clamp."""
    for ticks_per_step in (24, 12, 8, 6, 4, 3):
        limit = (ticks_per_step - 1) // 2
        for tick in range(ticks_per_step * 16):
            _, offset = quantize_hit(tick, ticks_per_step, 16)
            assert abs(offset) <= limit


def test_capture_is_accurate_to_within_one_tick_everywhere():
    """Across every tick and division, a captured hit plays back where it was.

    Boundary ticks may be nudged by one, which is the clamp doing its job;
    nothing may drift further than that.
    """
    for division, ticks_per_step in enumerate((24, 12, 8, 6, 4, 3)):
        song = Song(length=16, division=division)
        total = ticks_per_step * 16
        for tick in range(total):
            step, offset = quantize_hit(tick, ticks_per_step, 16)
            song.clear_all()
            song.set_step(0, step, 100, offset=offset)
            played = step_fires_at(song, 0, step, 0.0)
            error = (played - tick) % total
            if error > total // 2:
                error -= total
            assert abs(error) <= 1, (ticks_per_step, tick, played)


def test_every_tick_belongs_to_exactly_one_step():
    """No tick may be claimed by two steps, or by none."""
    for ticks_per_step in (24, 12, 8, 6, 4, 3):
        for length in (1, 8, 16, 64):
            total = ticks_per_step * length
            owners = {}
            for tick in range(total):
                step, offset = quantize_hit(tick, ticks_per_step, length)
                assert 0 <= step < length
                owners.setdefault(step, []).append(tick)
            assert sum(len(v) for v in owners.values()) == total


def test_capture_then_schedule_round_trips(song):
    """A hit recorded at a tick plays back on that tick at zero strength."""
    for tick in (0, 7, 26, 95):
        step, offset = quantize_hit(tick, song.ticks_per_step, song.length)
        song.clear_all()
        song.set_step(0, step, 100, offset=offset)
        assert step_fires_at(song, 0, step, 0.0) == tick
