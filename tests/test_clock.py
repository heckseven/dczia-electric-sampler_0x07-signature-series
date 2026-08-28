"""Tests for the 24 PPQN master clock.

engine.clock imports nothing from CircuitPython. Time is passed in, so these
tests drive the clock through exact simulated timelines rather than sleeping.
"""

import pytest

from engine.clock import (
    COUNT_WINDOW_BEATS,
    EXTERNAL,
    FLYWHEEL_AFTER_MS,
    INTERNAL,
    MAX_BPM,
    MAX_CATCHUP_TICKS,
    MIN_BPM,
    PPQN,
    SYNC_RATES,
    TICKS_MAX,
    TICKS_PERIOD,
    Clock,
    ticks_diff,
)
from engine.song import DIVISIONS


def run_for(clock, start, duration_ms, step_ms=1):
    """Poll the clock across a timeline the way the main loop would."""
    fired = 0
    now = start
    end = start + duration_ms
    while now <= end:
        fired += clock.update(now & TICKS_MAX)
        now += step_ms
    return fired


# --- wrap-safe time -------------------------------------------------------


def test_ticks_diff_on_ordinary_values():
    assert ticks_diff(500, 100) == 400


def test_ticks_diff_across_the_wrap():
    """supervisor.ticks_ms wraps at 2**29; a naive subtraction goes hugely negative."""
    before = TICKS_MAX - 5
    after = 4  # 10 ms later, having wrapped
    assert after - before < 0
    assert ticks_diff(after, before) == 10


def test_ticks_diff_is_signed_for_time_going_backwards():
    assert ticks_diff(100, 500) == -400


def test_clock_keeps_running_across_a_wrap():
    """The whole point of ticks_diff: a wrap must not stall playback."""
    clock = Clock(bpm=120)
    start = TICKS_PERIOD - 500
    clock.start(start & TICKS_MAX)
    fired = run_for(clock, start, 1000)
    expected = 1000 / clock.tick_period_ms
    assert abs(fired - expected) <= 1


# --- internal tempo -------------------------------------------------------


def test_tick_period_matches_the_tempo():
    clock = Clock(bpm=120)
    # 120 BPM is 2 quarters a second, 24 ticks each -> 48 ticks/s
    assert clock.tick_period_ms == pytest.approx(1000.0 / 48.0)


def test_a_stopped_clock_does_not_tick():
    clock = Clock(bpm=120)
    assert run_for(clock, 0, 1000) == 0
    assert clock.tick == 0


@pytest.mark.parametrize("bpm", [60, 120, 174, 300])
def test_tick_rate_is_accurate_over_a_second(bpm):
    clock = Clock(bpm=bpm)
    clock.start(0)
    fired = run_for(clock, 0, 1000)
    expected = bpm * PPQN / 60.0
    assert abs(fired - expected) <= 1


def test_no_drift_over_a_long_run():
    """Fractional remainder is carried, so error must not accumulate."""
    clock = Clock(bpm=137)
    clock.start(0)
    run_for(clock, 0, 60000, step_ms=1)
    expected = 137 * PPQN  # one minute of quarter notes
    assert abs(clock.tick - expected) <= 2


def test_bpm_is_clamped():
    clock = Clock()
    assert clock.set_bpm(1) == MIN_BPM
    assert clock.set_bpm(10000) == MAX_BPM


def test_a_coarse_polling_loop_still_keeps_time():
    """The main loop will not poll every millisecond."""
    clock = Clock(bpm=120)
    clock.start(0)
    fired = run_for(clock, 0, 1000, step_ms=7)
    assert abs(fired - 48) <= 2


def test_a_long_stall_does_not_fire_a_burst():
    """After a stall, drop the backlog rather than replaying it all at once."""
    clock = Clock(bpm=120)
    clock.start(0)
    fired = clock.update(5000)  # 5 seconds of nothing
    assert fired <= MAX_CATCHUP_TICKS


# --- step mapping ---------------------------------------------------------


@pytest.mark.parametrize("name,ticks", DIVISIONS)
def test_every_division_lands_on_whole_ticks(name, ticks):
    assert PPQN % ticks == 0


def test_step_advances_once_per_division():
    clock = Clock()
    ticks_per_step = 6  # 1/16
    seen = []
    for tick in range(0, 6 * 8):
        seen.append(clock.step_for_tick(ticks_per_step, 8, tick=tick))
    # Each step should be held for exactly 6 ticks, then wrap after 8 steps.
    assert seen[0:6] == [0] * 6
    assert seen[6:12] == [1] * 6
    assert seen[-1] == 7


def test_step_wraps_at_the_pattern_length():
    clock = Clock()
    assert clock.step_for_tick(6, 16, tick=6 * 16) == 0
    assert clock.step_for_tick(6, 16, tick=6 * 17) == 1


def test_step_boundaries_are_detected():
    clock = Clock()
    assert clock.is_step_boundary(6, tick=12) is True
    assert clock.is_step_boundary(6, tick=13) is False


# --- external sync --------------------------------------------------------


def test_clock_starts_internal():
    assert Clock().source == INTERNAL


def test_a_pulse_latches_the_clock_to_external():
    clock = Clock()
    clock.start(0)
    clock.external_pulse(100)
    assert clock.source == EXTERNAL


def test_tempo_is_measured_from_the_pulse_interval():
    """At 2 PPQN, 250ms between pulses is 120 BPM."""
    clock = Clock(bpm=60, sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    clock.external_pulse(1250)
    assert clock.bpm == pytest.approx(120, abs=0.5)


def send_pulse_train(clock, bpm, start=1000, count=None):
    """Feed a realistic stream of pulses at a given tempo.

    Timestamps are whole milliseconds, as supervisor.ticks_ms produces them.
    """
    if count is None:
        # Enough to fill the counting window a fast master is measured over,
        # and more than enough for the gap averaging a slow one uses.
        count = max(12, clock.sync_ppqn * COUNT_WINDOW_BEATS * 2 + 2)
    quarter_ms = 60000.0 / bpm
    interval = quarter_ms / clock.sync_ppqn
    for index in range(count):
        clock.external_pulse(int(round(start + index * interval)))


@pytest.mark.parametrize("rate", SYNC_RATES)
def test_every_sync_rate_measures_tempo_correctly(rate):
    clock = Clock(sync_ppqn=rate)
    clock.start(0)
    send_pulse_train(clock, 120)
    assert clock.bpm == pytest.approx(120, abs=2)


@pytest.mark.parametrize("bpm", [60, 120, 174])
def test_tempo_tracking_across_tempos(bpm):
    clock = Clock(sync_ppqn=2)
    clock.start(0)
    send_pulse_train(clock, bpm)
    assert clock.bpm == pytest.approx(bpm, abs=2)


def test_a_fast_sync_rate_is_measured_by_counting_not_by_gaps():
    """A single 24 PPQN interval is only millisecond-accurate: about 4% out.

    At 120 BPM the gap is 20.83 ms, so whole-millisecond timestamps round it
    to 20 or 21 and one interval alone reads as 125 or 119 BPM. Worse, a fast
    master is not read as it arrives - MIDI clock over USB is drained in
    bursts, so several pulses share a timestamp and the gap between them is
    zero. Counting pulses across a long span is immune to both, and measured
    on the badge it is the difference between reading a 150 BPM master as 150
    and reading it as 121.
    """
    single = Clock(sync_ppqn=24)
    single.start(0)
    single.external_pulse(1000)
    single.external_pulse(1020)
    assert single.bpm == 120, "two pulses is not a measurement"

    counted = Clock(sync_ppqn=24)
    counted.start(0)
    send_pulse_train(counted, 150)
    assert counted.bpm == pytest.approx(150, abs=3)


def test_a_fast_master_arriving_in_bursts_is_still_measured():
    """Several clocks sharing one timestamp is what a 20 ms poll produces."""
    clock = Clock(bpm=90)
    clock.start(0)
    now = 0
    sent = 0
    # 150 BPM is 60 clocks a second. Delivered eight at a time, that is a
    # burst every 133 ms - which is what a 20 ms poll draining what it finds
    # actually looks like from in here.
    burst = 8
    while sent < 24 * COUNT_WINDOW_BEATS * 2:
        now += 133
        for _ in range(burst):  # all at the same instant
            clock.external_pulse(now, ppqn=24)
            sent += 1
    assert 140 <= clock.bpm <= 160, clock.bpm


def test_tempo_window_scales_with_the_sync_rate():
    """A slow sync stays responsive; a fast one gets the averaging it needs."""
    assert Clock(sync_ppqn=2).pulse_window == 2
    assert Clock(sync_ppqn=4).pulse_window == 4
    assert Clock(sync_ppqn=24).pulse_window == 8


def test_the_knob_is_ignored_while_slaved():
    clock = Clock(bpm=120)
    clock.start(0)
    clock.external_pulse(100)
    clock.external_pulse(350)
    slaved = clock.bpm
    clock.set_bpm(200)
    assert clock.bpm == slaved


def test_stopping_releases_the_external_latch():
    clock = Clock(bpm=120)
    clock.start(0)
    clock.external_pulse(100)
    assert clock.source == EXTERNAL
    clock.stop()
    assert clock.source == INTERNAL
    assert clock.set_bpm(150) == 150


def test_absurd_pulse_gaps_are_ignored():
    """A noise spike or a restarted master must not throw the tempo."""
    clock = Clock(bpm=120, sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    clock.external_pulse(1250)
    sane = clock.bpm
    clock.external_pulse(1251)  # 1 ms apart: implausible
    assert clock.bpm == sane
    clock.external_pulse(9000)  # many seconds later: a restart, not a tempo
    assert clock.bpm == sane


def test_phase_snaps_to_an_arriving_pulse():
    clock = Clock(sync_ppqn=2)  # 12 ticks per pulse
    clock.start(0)
    clock.tick = 13  # one tick past a boundary
    clock.external_pulse(100)
    assert clock.tick % clock.ticks_per_pulse == 0
    assert clock.tick == 12


def test_phase_snaps_forward_when_that_is_nearer():
    clock = Clock(sync_ppqn=2)
    clock.start(0)
    clock.tick = 23  # one tick short of the next boundary
    clock.external_pulse(100)
    assert clock.tick == 24


# --- flywheel -------------------------------------------------------------


def test_clock_keeps_running_when_pulses_stop():
    """A master that pauses must not freeze the badge mid-pattern."""
    clock = Clock(bpm=60, sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    clock.external_pulse(1250)  # establishes 120 BPM
    before = clock.tick
    run_for(clock, 1250, 1000)  # a full second with no pulses
    assert clock.tick > before
    assert clock.source == EXTERNAL


def test_flywheel_runs_at_the_last_measured_tempo():
    clock = Clock(bpm=60, sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    clock.external_pulse(1250)  # 120 BPM
    fired = run_for(clock, 1250, 1000)
    assert abs(fired - 120 * PPQN / 60.0) <= 2


def test_flywheeling_is_reported_only_after_a_gap():
    clock = Clock(sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    assert clock.is_flywheeling(1000 + FLYWHEEL_AFTER_MS // 2) is False
    assert clock.is_flywheeling(1000 + FLYWHEEL_AFTER_MS + 10) is True


def test_an_internal_clock_never_reports_flywheeling():
    clock = Clock()
    clock.start(0)
    assert clock.is_flywheeling(100000) is False


def test_pulses_returning_after_a_gap_resynchronise():
    clock = Clock(bpm=60, sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    clock.external_pulse(1250)
    run_for(clock, 1250, 2000)  # long silence, flywheeling
    assert clock.is_flywheeling(3250) is True
    clock.external_pulse(3250)
    assert clock.is_flywheeling(3250) is False
    assert clock.tick % clock.ticks_per_pulse == 0


# --- sync output ----------------------------------------------------------


def test_sync_out_fires_at_the_configured_rate():
    clock = Clock(sync_ppqn=2)  # every 12 ticks
    due = [t for t in range(PPQN) if clock.sync_out_due(t)]
    assert due == [0, 12]


def test_sync_out_at_24_ppqn_fires_every_tick():
    clock = Clock(sync_ppqn=24)
    assert all(clock.sync_out_due(t) for t in range(PPQN))


def test_sync_rate_can_be_changed_and_rejects_junk():
    clock = Clock()
    assert clock.set_sync_ppqn(4) == 4
    assert clock.set_sync_ppqn(7) == 4


# --- a 24 PPQN master ------------------------------------------------------
#
# MIDI clock is fixed at 24 a quarter note by the standard, which is exactly
# this engine's tick rate, so one message is one tick. That is a different
# arrangement from the analog jack, where one pulse is worth several ticks and
# the clock free-runs between them - and getting the two confused is how a
# badge walks away from whatever it is following.


def test_a_24_ppqn_pulse_is_worth_exactly_one_tick():
    clock = Clock(bpm=120)
    clock.start(0)
    clock.external_pulse(1000, ppqn=24)  # the downbeat, where it already is
    before = clock.tick
    clock.external_pulse(1020, ppqn=24)
    assert clock.tick == before + 1


def test_the_first_clock_after_a_start_is_the_downbeat():
    """Not the step after it. Whatever is written on beat one must sound.

    Reported from the badge: four notes on the first bar, and the one on the
    downbeat stayed silent until the pattern came round again.
    """
    clock = Clock(bpm=120)
    clock.start(0)
    clock.external_pulse(1000, ppqn=24)
    assert clock.tick == 0, "the first clock moved off the downbeat"
    assert clock.update(1000) == 1, "and it never sounded"


def test_24_ppqn_pulses_do_not_also_free_run():
    """The pulses are the clock: one tick each, no more and none lost.

    update() is what the sequencer fires steps on, so both halves matter. Too
    many and the badge runs ahead of the master; too few and a step never
    sounds - which shows up as a pattern that looks like it is playing and
    makes no noise at all.
    """
    clock = Clock(bpm=120)
    clock.start(0)
    now = 0
    fired = 0
    for _ in range(24):
        now += 20
        clock.external_pulse(now, ppqn=24)
        fired += clock.update(now + 10)
        # A second poll before the next pulse must add nothing.
        assert clock.update(now + 15) == 0, "it manufactured a tick of its own"
    assert fired == 24, "a tick never came back out of update"
    # 23 rather than 24: the first clock sounded the downbeat the playhead
    # was already on rather than moving past it.
    assert clock.tick == 23


def test_every_pulse_driven_tick_is_handed_to_the_caller():
    """A tick that does not come out of update is a step that never sounds."""
    clock = Clock(bpm=120)
    clock.start(0)
    now = 0
    fired = 0
    for _ in range(96):
        now += 20
        clock.external_pulse(now, ppqn=24)
        fired += clock.update(now)
    assert fired == 96, "a tick never came back out of update"
    assert clock.tick == 95, "the first clock is the downbeat, not the step after"


def test_pulses_arriving_between_polls_are_not_lost():
    """Several clocks can land in one 2 ms poll; each is still a step."""
    clock = Clock(bpm=120)
    clock.start(0)
    now = 0
    for _ in range(4):  # a burst, all before the next update
        now += 20
        clock.external_pulse(now, ppqn=24)
    assert clock.update(now) == 4


def test_a_24_ppqn_master_sets_the_tempo():
    clock = Clock(bpm=90)
    clock.start(0)
    now = 0.0
    # 150 BPM at 24 PPQN is one clock every 16.67 ms. The tempo is counted
    # over a window of beats, so send more than one of them.
    for _ in range(24 * COUNT_WINDOW_BEATS * 2):
        now += 16.67
        clock.external_pulse(int(now), ppqn=24)
    assert clock.source == "ext"
    assert 145 <= clock.bpm <= 155, clock.bpm


def test_the_jacks_rate_does_not_change_what_a_midi_clock_means():
    """The two can differ, and only one of them is a MIDI cable."""
    clock = Clock(bpm=120, sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000, ppqn=24)  # the downbeat
    before = clock.tick
    clock.external_pulse(1020, ppqn=24)
    assert clock.tick == before + 1, "it used the jack's rate"


def test_an_analog_pulse_still_carries_several_ticks():
    """The 24 PPQN path must not have changed the jack's behaviour."""
    clock = Clock(bpm=120, sync_ppqn=2)
    clock.start(0)
    clock.tick = 5
    clock.external_pulse(1000)
    assert clock.tick % clock.ticks_per_pulse == 0, "the phase was not snapped"


def test_an_analog_master_still_free_runs_between_pulses():
    clock = Clock(bpm=120, sync_ppqn=2)
    clock.start(0)
    clock.external_pulse(1000)
    assert clock.update(1200) > 0, "nothing ticked between two analog pulses"


def test_a_master_that_stops_lets_the_clock_free_run_again():
    """Flywheeling: it keeps playing on the last tempo it measured."""
    clock = Clock(bpm=120)
    clock.start(0)
    now = 0
    for _ in range(8):
        now += 20
        clock.external_pulse(now, ppqn=24)
    quiet = now + FLYWHEEL_AFTER_MS + 500
    assert clock.is_flywheeling(quiet) is True
    assert clock.update(quiet) > 0, "it stopped dead when the clocks stopped"


def test_changing_master_discards_the_tempo_measured_from_the_other():
    """A history gathered at one rate means nothing at another."""
    clock = Clock(bpm=120, sync_ppqn=2)
    clock.start(0)
    now = 0
    for _ in range(4):
        now += 250
        clock.external_pulse(now)
    at_two = clock.bpm
    for _ in range(24 * COUNT_WINDOW_BEATS * 2):
        now += 10  # 250 BPM at 24 PPQN
        clock.external_pulse(now, ppqn=24)
    assert clock.bpm != at_two
    assert clock.bpm > 200, clock.bpm


def test_stopping_forgets_which_master_was_driving():
    clock = Clock(bpm=120)
    clock.start(0)
    clock.external_pulse(1000, ppqn=24)
    clock.stop()
    clock.start(2000)
    before = clock.tick
    clock.update(2100)
    assert clock.tick > before, "it stayed pulse driven with no master"
