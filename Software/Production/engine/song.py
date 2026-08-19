"""Pattern data for the sampler.

Storage is deliberately flat. Each track holds one byte per step for velocity
and one byte per step for micro-timing, so a full 8 track by 64 step pattern
costs 1KB of contiguous bytearrays:

    steps[track][step]    0 = off, 1..127 = velocity
    offsets[track][step]  128 = on the grid, +/- ticks either side

The obvious alternative, a list of [on, velocity] pairs per step, costs a list
object and a boxed float for every one of the 512 steps - tens of kilobytes on
a board with roughly 140KB free, and it fragments the heap badly enough to
matter. Bytearrays avoid both.

Offsets are stored biased by 128 because a bytearray holds unsigned values.
They are clamped to half a step either side, which means the nearest pad to a
recorded hit is always the step it is stored in, so the display never has to
search for which pad to light.

This module imports nothing from CircuitPython so it can be tested directly.
"""

TRACK_COUNT = 8
MAX_STEPS = 64
STEPS_PER_PAGE = 8
PAGE_COUNT = MAX_STEPS // STEPS_PER_PAGE

OFF = 0
MIN_VELOCITY = 1
MAX_VELOCITY = 127
DEFAULT_VELOCITY = 100

OFFSET_BIAS = 128

MIN_LENGTH = 1
MIN_BPM = 20
MAX_BPM = 300

# Ticks per step at 24 PPQN. Every entry divides evenly, so no division drifts.
DIVISIONS = (
    ("1/4", 24),
    ("1/8", 12),
    ("1/8T", 8),
    ("1/16", 6),
    ("1/16T", 4),
    ("1/32", 3),
)
DEFAULT_DIVISION = 3  # 1/16


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


class Song:
    """One pattern: step data for 8 tracks, plus the kit they play."""

    def __init__(self, length=16, division=DEFAULT_DIVISION, bpm=120):
        self.steps = [bytearray(MAX_STEPS) for _ in range(TRACK_COUNT)]
        self.offsets = [bytearray(b"\x80" * MAX_STEPS) for _ in range(TRACK_COUNT)]
        # Sample path per track; None means the track has no sound loaded.
        self.kit = [None] * TRACK_COUNT
        self.kit_name = None
        self.muted = [False] * TRACK_COUNT
        self._length = clamp(length, MIN_LENGTH, MAX_STEPS)
        self._division = clamp(division, 0, len(DIVISIONS) - 1)
        self._bpm = clamp(bpm, MIN_BPM, MAX_BPM)

    # --- pattern shape ----------------------------------------------------

    @property
    def length(self):
        return self._length

    def set_length(self, steps):
        self._length = clamp(steps, MIN_LENGTH, MAX_STEPS)
        return self._length

    @property
    def division(self):
        return self._division

    def set_division(self, index):
        self._division = clamp(index, 0, len(DIVISIONS) - 1)
        # A coarser grid allows larger offsets; a finer one must not keep
        # offsets that now reach past the neighbouring step.
        self._reclamp_offsets()
        return self._division

    @property
    def division_name(self):
        return DIVISIONS[self._division][0]

    @property
    def ticks_per_step(self):
        return DIVISIONS[self._division][1]

    @property
    def max_offset(self):
        """The furthest a hit can sit from its own grid line.

        Strictly less than half a step, not exactly half. A hit at exactly
        half a step is equidistant between two grid lines, so scheduling
        cannot say which step owns it: the tick gets attributed to the
        neighbour and the hit never fires, and two steps can claim the same
        tick. One tick short of the boundary keeps every tick owned by
        exactly one step at every division.
        """
        return (self.ticks_per_step - 1) // 2

    @property
    def bpm(self):
        return self._bpm

    def set_bpm(self, value):
        self._bpm = clamp(int(value), MIN_BPM, MAX_BPM)
        return self._bpm

    @property
    def page_count(self):
        """Pages needed to show the pattern, 8 steps to a page."""
        return (self._length + STEPS_PER_PAGE - 1) // STEPS_PER_PAGE

    # --- steps ------------------------------------------------------------

    def is_on(self, track, step):
        return self.steps[track][step] != OFF

    def velocity(self, track, step):
        return self.steps[track][step]

    def set_step(self, track, step, velocity=DEFAULT_VELOCITY, offset=0):
        self.steps[track][step] = clamp(int(velocity), OFF, MAX_VELOCITY)
        self.set_offset(track, step, offset)
        return self.steps[track][step]

    def clear_step(self, track, step):
        self.steps[track][step] = OFF
        self.offsets[track][step] = OFFSET_BIAS

    def toggle_step(self, track, step, velocity=DEFAULT_VELOCITY):
        """Tap behaviour: a lit pad clears, an unlit pad records a hit."""
        if self.is_on(track, step):
            self.clear_step(track, step)
            return False
        self.set_step(track, step, velocity)
        return True

    def set_velocity(self, track, step, velocity):
        """Change a step's level without moving it on or off the grid."""
        if not self.is_on(track, step):
            return OFF
        self.steps[track][step] = clamp(int(velocity), MIN_VELOCITY, MAX_VELOCITY)
        return self.steps[track][step]

    def clear_track(self, track):
        for step in range(MAX_STEPS):
            self.steps[track][step] = OFF
            self.offsets[track][step] = OFFSET_BIAS

    def clear_all(self):
        for track in range(TRACK_COUNT):
            self.clear_track(track)

    def is_empty(self):
        for track in range(TRACK_COUNT):
            for step in range(self._length):
                if self.steps[track][step] != OFF:
                    return False
        return True

    # --- micro-timing -----------------------------------------------------

    def offset(self, track, step):
        """Signed tick offset from this step's grid line."""
        return self.offsets[track][step] - OFFSET_BIAS

    def set_offset(self, track, step, ticks):
        limit = self.max_offset
        self.offsets[track][step] = OFFSET_BIAS + clamp(int(ticks), -limit, limit)
        return self.offset(track, step)

    def _reclamp_offsets(self):
        limit = self.max_offset
        for track in range(TRACK_COUNT):
            row = self.offsets[track]
            for step in range(MAX_STEPS):
                value = row[step] - OFFSET_BIAS
                if value > limit:
                    row[step] = OFFSET_BIAS + limit
                elif value < -limit:
                    row[step] = OFFSET_BIAS - limit

    # --- tracks -----------------------------------------------------------

    def set_sample(self, track, path):
        self.kit[track] = path

    def toggle_mute(self, track):
        self.muted[track] = not self.muted[track]
        return self.muted[track]

    # --- persistence ------------------------------------------------------

    def to_dict(self):
        """A msgpack-friendly form: bytes rather than lists of ints."""
        return {
            "v": 1,
            "length": self._length,
            "division": self._division,
            "bpm": self._bpm,
            "kit_name": self.kit_name,
            "kit": list(self.kit),
            "muted": list(self.muted),
            "steps": [bytes(row) for row in self.steps],
            "offsets": [bytes(row) for row in self.offsets],
        }

    @classmethod
    def from_dict(cls, data):
        song = cls(
            length=data.get("length", 16),
            division=data.get("division", DEFAULT_DIVISION),
            bpm=data.get("bpm", 120),
        )
        song.kit_name = data.get("kit_name")
        kit = data.get("kit") or []
        for track in range(min(TRACK_COUNT, len(kit))):
            song.kit[track] = kit[track]
        muted = data.get("muted") or []
        for track in range(min(TRACK_COUNT, len(muted))):
            song.muted[track] = bool(muted[track])
        for name in ("steps", "offsets"):
            rows = data.get(name) or []
            target = song.steps if name == "steps" else song.offsets
            for track in range(min(TRACK_COUNT, len(rows))):
                row = rows[track]
                for step in range(min(MAX_STEPS, len(row))):
                    target[track][step] = row[step]
        return song
