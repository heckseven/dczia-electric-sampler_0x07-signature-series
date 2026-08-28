"""The sampler's master clock.

Runs at 24 pulses per quarter note. Every step division the sequencer offers
divides 24 exactly - 1/4 is 24 ticks, 1/8 is 12, 1/8T is 8, 1/16 is 6, 1/16T is
4, 1/32 is 3 - so no grid accumulates rounding error against another.

The clock is polled, never blocking. update() is handed the current time and
reports how many ticks have elapsed since the last call, so the main loop stays
free to read keys and redraw while a pattern plays. The old sequencer spun in
`while ticks_ms() < deadline: pass` for the whole of every step, which is why
input was dropped and the display could not be touched during playback.

Two clock sources:

* Internal, where the tick period comes from the tempo.
* External, taken from pulses on the sync jack. The first pulse latches the
  clock to external until the transport stops. Tempo is measured from the gap
  between pulses, and the phase is snapped to each arriving pulse.

If external pulses stop arriving the clock does not stall: it keeps running at
the last tempo it measured, and re-synchronises when pulses return. A master
that pauses briefly, or a flaky cable, does not halt the badge mid-pattern.

Time is passed in rather than read here, so this module imports nothing from
CircuitPython and can be tested directly.
"""

from engine.util import clamp

PPQN = 24

# supervisor.ticks_ms wraps at 2**29 ms, roughly every 6.2 days, and the
# adafruit_ticks helper is not in this firmware build. Comparing raw values
# across a wrap would produce a vast negative interval and stall the clock, so
# all time arithmetic goes through ticks_diff.
TICKS_PERIOD = 1 << 29
TICKS_MAX = TICKS_PERIOD - 1
TICKS_HALFPERIOD = TICKS_PERIOD // 2

INTERNAL = "int"
EXTERNAL = "ext"

# Pulses per quarter note the sync jacks can speak. 2 is the Volca and Pocket
# Operator convention and the default; 24 matches MIDI clock and DIN sync.
SYNC_RATES = (1, 2, 4, 24)
DEFAULT_SYNC_PPQN = 2

MIN_BPM = 20
MAX_BPM = 300

# Pulse gaps outside this range are treated as noise or a restarted master
# rather than a tempo, so a glitch cannot throw the clock to an absurd speed.
MIN_PULSE_MS = 2
MAX_PULSE_MS = 3000

# Tempo is averaged over several pulses rather than taken from one gap.
# Timestamps arrive in whole milliseconds, so a single interval is only as
# accurate as that resolution allows: at 2 PPQN the gap is 250 ms at 120 BPM and
# 1 ms of error is 0.4%, but at 24 PPQN the gap is 20.8 ms and the same 1 ms is
# nearly 5%. Averaging across a window divides that error by the number of
# intervals in it. The window scales with the rate so a slow sync stays
# responsive to tempo changes while a fast one gets the accuracy it needs.
MIN_PULSE_WINDOW = 2
MAX_PULSE_WINDOW = 8

# How many quarter notes a pulse-driven master's tempo is counted over, and
# how often that count is read.
#
# The error is whatever displaced the two timestamps, against the whole span.
# Measured against a real MIDI master over DIN: a two beat baseline reported
# 131 to 156 BPM for a steady 137.6, because the occasional 34 to 46 ms pass -
# a collection, a redraw - lands on one end of the window and 46 ms of 872 is
# 5%. Eight beats is four times the baseline for the same displacement, and
# reading it every beat keeps it responsive despite the longer memory.
COUNT_WINDOW_BEATS = 8
REPORT_EVERY_BEATS = 1

# How much of the previous reading to keep. A little smoothing settles the
# number on screen without slowing a real tempo change much: a master that
# jumps tempo lands within a couple of readings, which is a couple of beats.
BPM_SMOOTHING = 0.5

# After this long with no pulse the clock is running on its own memory of the
# tempo. It keeps playing; this only drives the "flywheeling" indicator.
FLYWHEEL_AFTER_MS = 1000

# A stall (garbage collection, a slow redraw) must not produce a burst of
# catch-up ticks that all fire in one update.
MAX_CATCHUP_TICKS = 8


def ticks_diff(later, earlier):
    """Signed millisecond difference that survives the 2**29 wrap."""
    diff = (later - earlier) & TICKS_MAX
    return ((diff + TICKS_HALFPERIOD) & TICKS_MAX) - TICKS_HALFPERIOD


class Clock:
    def __init__(self, bpm=120, sync_ppqn=DEFAULT_SYNC_PPQN):
        self._bpm = clamp(bpm, MIN_BPM, MAX_BPM)
        self.sync_ppqn = sync_ppqn if sync_ppqn in SYNC_RATES else DEFAULT_SYNC_PPQN
        self.tick = 0
        self.running = False
        self.source = INTERNAL
        self._accum = 0.0
        self._last_update = 0
        self._last_pulse = None
        self._pulse_history = []
        # The rate of whatever is driving the external sync, which is not
        # always the jack's. MIDI clock is fixed at 24 PPQN by the standard;
        # the analog input is whatever the player set.
        self._external_ppqn = None
        # For a pulse-driven master, tempo is counted rather than timed
        # between pulses - see external_pulse.
        self._pulse_epoch = None
        self._pulse_count = 0
        # Ticks advanced by a pulse and not yet handed to the caller. They
        # have to come out of update() like every other tick, because that
        # return value is what the sequencer fires steps on.
        self._pending_ticks = 0
        # Set when the tick the clock is sitting on has not been played yet.
        # Starting puts the playhead at position zero, and position zero is a
        # step like any other - without this the first tick handed over is 1
        # and whatever is written on the downbeat is silent until the pattern
        # comes round again.
        self._fire_current = False
        # True when one pulse means exactly one tick, which is the 24 PPQN
        # case. Then the pulses are the clock and nothing is free-run
        # between them - see update().
        self._pulse_driven = False

    # --- tempo ------------------------------------------------------------

    @property
    def bpm(self):
        return self._bpm

    def set_bpm(self, value):
        """Set the internal tempo. Ignored while slaved to an external clock."""
        if self.source == EXTERNAL:
            return self._bpm
        self._bpm = clamp(int(value), MIN_BPM, MAX_BPM)
        return self._bpm

    @property
    def tick_period_ms(self):
        return 60000.0 / (self._bpm * PPQN)

    # --- transport --------------------------------------------------------

    def start(self, now):
        self.running = True
        self._last_update = now
        self._accum = 0.0
        # Wherever the playhead is, it has not sounded yet.
        self._fire_current = True

    def stop(self):
        """Stop, and release any external latch so the knob works again."""
        self.running = False
        self.source = INTERNAL
        self._last_pulse = None
        self._pulse_history = []
        # The rate of whatever is driving the external sync, which is not
        # always the jack's. MIDI clock is fixed at 24 PPQN by the standard;
        # the analog input is whatever the player set.
        self._external_ppqn = None
        # For a pulse-driven master, tempo is counted rather than timed
        # between pulses - see external_pulse.
        self._pulse_epoch = None
        self._pulse_count = 0
        # Ticks advanced by a pulse and not yet handed to the caller. They
        # have to come out of update() like every other tick, because that
        # return value is what the sequencer fires steps on.
        self._pending_ticks = 0
        # Set when the tick the clock is sitting on has not been played yet.
        # Starting puts the playhead at position zero, and position zero is a
        # step like any other - without this the first tick handed over is 1
        # and whatever is written on the downbeat is silent until the pattern
        # comes round again.
        self._fire_current = False
        # True when one pulse means exactly one tick, which is the 24 PPQN
        # case. Then the pulses are the clock and nothing is free-run
        # between them - see update().
        self._pulse_driven = False
        self._accum = 0.0

    def reset(self):
        self.tick = 0
        self._accum = 0.0
        self._pending_ticks = 0
        self._fire_current = True

    # --- the poll ---------------------------------------------------------

    def update(self, now):
        """Advance the clock. Returns how many ticks fired since the last call."""
        if not self.running:
            self._last_update = now
            return 0

        # Handled before anything to do with elapsed time, and deliberately.
        # The caller passes one `now` to the MIDI poll and to this in the same
        # pass, so the pulse that just arrived set _last_update to exactly
        # this value - and an elapsed-time check would see zero and return
        # early, dropping the tick before it was ever handed over. That is a
        # pattern whose playhead moves and which makes no sound.
        if self._pulse_driven and not self.is_flywheeling(now):
            # A 24 PPQN master sends one clock per tick, so the clocks are the
            # tick and generating more here would race them. Which way it
            # raced would depend on whether the measured tempo came out a
            # hair fast or slow: fast and the ticks double up, slow and the
            # accumulator is zeroed by the next pulse before its tick ever
            # fires, losing it. Either way the badge walks away from the
            # master. Free-running resumes the moment the clocks stop, which
            # is what flywheeling is.
            self._accum = 0.0
            self._last_update = now
            fired = self._pending_ticks
            self._pending_ticks = 0
            return fired

        fired = 0
        if self._fire_current:
            # The tick already under the playhead, played before any time is
            # counted against it. Nothing is added to self.tick here: this is
            # that position sounding, not the next one arriving.
            #
            # Above the elapsed check deliberately, and for the same reason
            # the pulse-driven branch is: start() sets _last_update to its
            # own `now`, so the first poll afterwards can measure zero
            # elapsed and return early - which would swallow the downbeat
            # exactly as before.
            self._fire_current = False
            fired += 1

        elapsed = ticks_diff(now, self._last_update)
        self._last_update = now
        if elapsed <= 0:
            return fired

        period = self.tick_period_ms
        self._accum += elapsed
        while self._accum >= period and fired < MAX_CATCHUP_TICKS:
            self._accum -= period
            self.tick += 1
            fired += 1
        if fired >= MAX_CATCHUP_TICKS:
            # Fell far behind; drop the backlog rather than firing a burst.
            self._accum = 0.0
        return fired

    # --- external sync ----------------------------------------------------

    @property
    def ticks_per_pulse(self):
        return PPQN // self.sync_ppqn

    def set_sync_ppqn(self, rate):
        if rate in SYNC_RATES:
            self.sync_ppqn = rate
            self._pulse_history = []
        return self.sync_ppqn

    @property
    def pulse_window(self):
        """How many pulses to average tempo over at the current sync rate."""
        rate = self._external_ppqn or self.sync_ppqn
        return clamp(rate, MIN_PULSE_WINDOW, MAX_PULSE_WINDOW)

    def external_pulse(self, now, ppqn=None):
        """Handle one pulse from whatever is driving the clock.

        Latches the clock to external, takes the tempo from the gap since the
        previous pulse, and snaps the phase so this pulse lands on a boundary.

        `ppqn` is how many of these arrive per quarter note, and it is not
        always the jack's rate: MIDI clock is fixed at 24 by the standard,
        while the analog input is whatever the player selected. Passing it
        rather than reading self.sync_ppqn is what lets both drive the same
        clock without either having to know about the other.

        At 24 the pulse is worth exactly one tick, so it is counted as one
        here rather than left to update() - see the note there.
        """
        if ppqn is None:
            ppqn = self.sync_ppqn
        if self._external_ppqn != ppqn:
            # A different master, or the first pulse from this one. The
            # history was measured against another rate and means nothing now.
            self._pulse_history = []
            self._last_pulse = None
            self._pulse_epoch = None
            self._pulse_count = 0
            self._external_ppqn = ppqn
        per_pulse = PPQN // ppqn if ppqn else 1
        self._pulse_driven = per_pulse <= 1
        if self._pulse_driven:
            self._measure_by_counting(now, ppqn)
        elif self._last_pulse is None:
            self._pulse_history = [now]
        else:
            gap = ticks_diff(now, self._last_pulse)
            if gap < MIN_PULSE_MS or gap > MAX_PULSE_MS:
                # Noise, or a master that stopped and restarted. Discard the
                # history and keep the tempo already measured rather than
                # believing a gap that cannot be a tempo.
                self._pulse_history = [now]
            else:
                self._pulse_history.append(now)
                while len(self._pulse_history) > self.pulse_window:
                    self._pulse_history.pop(0)
                if len(self._pulse_history) >= 2:
                    span = ticks_diff(self._pulse_history[-1], self._pulse_history[0])
                    intervals = len(self._pulse_history) - 1
                    average = span / float(intervals)
                    measured = 60000.0 / (average * ppqn)
                    self._bpm = clamp(measured, MIN_BPM, MAX_BPM)
        self._last_pulse = now
        self.source = EXTERNAL

        if self._pulse_driven:
            # One pulse, one tick. Nothing to snap to and nothing generating
            # ticks in between, so this is the whole of the clock.
            #
            # The first pulse after a start is the downbeat rather than the
            # step after it, which is what the MIDI standard means by Start
            # followed by a clock - so it sounds where the playhead already
            # is instead of moving it on.
            if self._fire_current:
                self._fire_current = False
            else:
                self.tick += 1
            if self._pending_ticks < MAX_CATCHUP_TICKS:
                self._pending_ticks += 1
        else:
            # Snap to the nearest pulse boundary. Drift between pulses is
            # small because the clock free-runs at the measured tempo, so
            # this is a correction of a tick or two rather than an audible
            # jump.
            remainder = self.tick % per_pulse
            if remainder:
                if remainder * 2 >= per_pulse:
                    self.tick += per_pulse - remainder
                else:
                    self.tick -= remainder
        self._accum = 0.0
        # The span since the last poll has been consumed by the snap above.
        # Without this, update() would count it a second time and fire a burst
        # of catch-up ticks.
        self._last_update = now

    def _measure_by_counting(self, now, ppqn):
        """Tempo from how many pulses arrived over how long, not from gaps.

        A fast master is not read as it arrives. MIDI clock over USB is
        collected on a 20 ms timer and drained in bursts, so several clocks
        share one timestamp and the gap between them reads as zero - which
        the gap check below throws away as noise, leaving the tempo measured
        from the poll interval rather than from the music. Measured on the
        badge: a 150 BPM master read as 121.

        Counting is immune to that. However bunched the arrivals are, the
        number of them is exact and the span is long, so the average holds
        even when no single interval does. Over a window this size the error
        is the timestamp resolution against most of a second.
        """
        if self._pulse_epoch is None:
            self._pulse_epoch = now
            self._pulse_count = 0
            return
        self._pulse_count += 1
        if self._pulse_count % (ppqn * REPORT_EVERY_BEATS):
            return
        span = ticks_diff(now, self._pulse_epoch)
        if span >= MIN_PULSE_MS:
            quarters = self._pulse_count / float(ppqn)
            measured = clamp(60000.0 * quarters / span, MIN_BPM, MAX_BPM)
            if self._bpm and self.source == EXTERNAL:
                measured = measured * (1.0 - BPM_SMOOTHING) + self._bpm * BPM_SMOOTHING
            self._bpm = clamp(measured, MIN_BPM, MAX_BPM)
        if self._pulse_count >= ppqn * COUNT_WINDOW_BEATS:
            # Start a fresh baseline. Keeping one for ever would make the
            # clock deaf to a tempo change; this bounds how far back it
            # remembers while still measuring across most of that.
            self._pulse_epoch = now
            self._pulse_count = 0

    def is_flywheeling(self, now):
        """External clock latched, but running on the last measured tempo."""
        if self.source != EXTERNAL or self._last_pulse is None:
            return False
        return ticks_diff(now, self._last_pulse) > FLYWHEEL_AFTER_MS

    # --- sync output ------------------------------------------------------

    def sync_out_due(self, tick=None):
        """True when this tick lands on a sync-output pulse."""
        if tick is None:
            tick = self.tick
        return tick % self.ticks_per_pulse == 0

    # --- step mapping -----------------------------------------------------

    def step_for_tick(self, ticks_per_step, length, tick=None):
        if tick is None:
            tick = self.tick
        return (tick // ticks_per_step) % length

    def is_step_boundary(self, ticks_per_step, tick=None):
        if tick is None:
            tick = self.tick
        return tick % ticks_per_step == 0
