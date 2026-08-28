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

from engine.util import clamp

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

# What a new song is. Eight steps is one page, so the eight pads are the
# whole pattern and there is no paging to understand before you can write a
# beat. At the default 1/16 division that is half a bar of 4/4, which is the
# trade: the pads no longer line up with anything you would count, but what
# you see is all there is.
DEFAULT_LENGTH = 8

# Per-track loudness, as a multiplier. One is the sample as recorded. The
# ceiling is above one so a quiet sample can be brought up rather than only
# pulled down; the mixer sums voices, so the sum still has to fit - see the
# level budget in sequencer.py.
MIN_TRACK_VOLUME = 0.0
MAX_TRACK_VOLUME = 2.0
DEFAULT_TRACK_VOLUME = 1.0
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


class Song:
    """One pattern: step data for 8 tracks, plus the kit they play."""

    def __init__(self, length=DEFAULT_LENGTH, division=DEFAULT_DIVISION, bpm=120):
        self.steps = [bytearray(MAX_STEPS) for _ in range(TRACK_COUNT)]
        self.offsets = [bytearray(b"\x80" * MAX_STEPS) for _ in range(TRACK_COUNT)]
        # Sample path per track; None means the track has no sound loaded.
        self.kit = [None] * TRACK_COUNT
        self.kit_name = None
        self.muted = [False] * TRACK_COUNT
        # Per-track quantise strength. None means "follow the global setting",
        # which is what every track does until one is deliberately given its
        # own feel - a swung hat over a straight kick, say.
        self.track_strength = [None] * TRACK_COUNT
        # How loud each track is, as a multiplier on the hit's own velocity.
        # None means "no opinion", and the kit's baseline is used instead -
        # see kit_volume. Turning the knob is what sets one.
        self.track_volume = [None] * TRACK_COUNT
        # The baseline that came with the kit, for tracks the song has no
        # opinion about. A too-loud kick is a property of the sample rather
        # than of the arrangement, so it travels with the sounds.
        self.kit_volume = [DEFAULT_TRACK_VOLUME] * TRACK_COUNT
        # Every track has its own length, so tracks of different lengths
        # drift against each other and realign on their own cycle. A
        # sixteen-step kick under a twelve-step hat repeats every forty-eight
        # steps without either pattern being written out that long.
        #
        # A bytearray because the values are 1..64 and there are eight of
        # them, and because that is what the rest of this model already
        # does with per-step data.
        self._lengths = bytearray([clamp(length, MIN_LENGTH, MAX_STEPS)] * TRACK_COUNT)
        self._division = clamp(division, 0, len(DIVISIONS) - 1)
        self._bpm = clamp(bpm, MIN_BPM, MAX_BPM)

    # --- pattern shape ----------------------------------------------------

    @property
    def length(self):
        """The longest track, which is how far the pattern reaches.

        There is no single song length once tracks differ, so this is the
        one that matters for anything asking how big the pattern is: the
        page count, and how much of it a display has to be able to show.
        """
        return max(self._lengths)

    @property
    def lengths(self):
        return self._lengths

    def track_length(self, track):
        return self._lengths[track]

    def set_length(self, steps):
        """Set every track's length. This is what the Global setting does."""
        steps = clamp(steps, MIN_LENGTH, MAX_STEPS)
        for track in range(TRACK_COUNT):
            self._lengths[track] = steps
        return steps

    def set_track_length(self, track, steps):
        """Set one track's length, leaving the others where they are."""
        steps = clamp(steps, MIN_LENGTH, MAX_STEPS)
        self._lengths[track] = steps
        return steps

    @property
    def uniform_length(self):
        """Whether every track is still the same length.

        Worth asking before showing a single number for "the" length: once
        tracks differ, one number is a lie.
        """
        return len(set(self._lengths)) == 1

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

    def page_count_for(self, track):
        """Pages needed to show one track, 8 steps to a page."""
        return (self._lengths[track] + STEPS_PER_PAGE - 1) // STEPS_PER_PAGE

    @property
    def page_count(self):
        """Pages needed to show the longest track."""
        return (self.length + STEPS_PER_PAGE - 1) // STEPS_PER_PAGE

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
            for step in range(self._lengths[track]):
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

    # --- per-track loudness -----------------------------------------------

    def volume_for(self, track):
        """What this track's hits are scaled by, song first then kit."""
        chosen = self.track_volume[track]
        if chosen is None:
            return self.kit_volume[track]
        return chosen

    def set_track_volume(self, track, value):
        """Give the song an opinion about one track. Clamped."""
        self.track_volume[track] = _clamp_volume(value)
        return self.track_volume[track]

    def clear_track_volume(self, track):
        """Forget the song's opinion, falling back to the kit's."""
        self.track_volume[track] = None

    def set_kit_volume(self, track, value):
        self.kit_volume[track] = _clamp_volume(value)
        return self.kit_volume[track]

    def capture_kit_volumes(self):
        """Make what is currently sounding the kit's baseline.

        What `Kit > Save` writes: balance the tracks by ear, then save, and
        the balance travels with the sounds rather than with the song.
        """
        return [self.volume_for(track) for track in range(TRACK_COUNT)]

    def toggle_mute(self, track):
        self.muted[track] = not self.muted[track]
        return self.muted[track]

    # --- per-track settings -----------------------------------------------

    def strength_for(self, track, global_strength):
        """The quantise strength this track plays at. Track overrides global."""
        override = self.track_strength[track]
        return global_strength if override is None else override

    def set_track_strength(self, track, value):
        """Give a track its own strength, or pass None to follow the global."""
        if value is None:
            self.track_strength[track] = None
        else:
            self.track_strength[track] = clamp(value, 0.0, 1.0)
        return self.track_strength[track]

    def has_track_strength(self, track):
        return self.track_strength[track] is not None

    # --- persistence ------------------------------------------------------

    def to_dict(self):
        """A msgpack-friendly form: bytes rather than lists of ints."""
        return {
            "v": 1,
            "length": self.length,
            "lengths": bytes(self._lengths),
            "division": self._division,
            "bpm": self._bpm,
            "kit_name": self.kit_name,
            "kit": list(self.kit),
            "muted": list(self.muted),
            "track_strength": list(self.track_strength),
            "track_volume": list(self.track_volume),
            "kit_volume": list(self.kit_volume),
            "steps": [bytes(row) for row in self.steps],
            "offsets": [bytes(row) for row in self.offsets],
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild a song from a decoded file.

        Everything here came off an SD card the badge does not control, so no
        value is trusted: each field is coerced to the type it must be and
        clamped to the range the rest of the engine assumes. A file that is
        corrupt, hand-edited, or written by a different version should load
        as a slightly wrong song, never as an exception on the main loop.
        """
        song = cls(
            length=data.get("length", DEFAULT_LENGTH),
            division=data.get("division", DEFAULT_DIVISION),
            bpm=data.get("bpm", 120),
        )
        lengths = data.get("lengths")
        if lengths:
            for track in range(min(TRACK_COUNT, len(lengths))):
                song.set_track_length(track, _as_int(lengths[track], song.length))
        song.kit_name = _as_name(data.get("kit_name"))
        kit = data.get("kit") or []
        for track in range(min(TRACK_COUNT, len(kit))):
            # A kit entry ends up at open() and at name.startswith("/"), so
            # anything that is not a string has to be dropped here rather
            # than raising somewhere further away.
            song.kit[track] = _as_name(kit[track])
        muted = data.get("muted") or []
        for track in range(min(TRACK_COUNT, len(muted))):
            song.muted[track] = bool(muted[track])
        strengths = data.get("track_strength") or []
        for track in range(min(TRACK_COUNT, len(strengths))):
            song.set_track_strength(track, _as_number(strengths[track], 1.0))
        # None is a real value here and means "no opinion, use the kit's", so
        # it survives rather than being coerced to a number.
        volumes = data.get("track_volume") or []
        for track in range(min(TRACK_COUNT, len(volumes))):
            if volumes[track] is None:
                song.clear_track_volume(track)
            else:
                song.set_track_volume(
                    track, _as_number(volumes[track], DEFAULT_TRACK_VOLUME)
                )
        kit_volumes = data.get("kit_volume") or []
        for track in range(min(TRACK_COUNT, len(kit_volumes))):
            song.set_kit_volume(
                track, _as_number(kit_volumes[track], DEFAULT_TRACK_VOLUME)
            )
        # Copy through the same clamps every other write path uses. A file
        # written by another version, or a corrupted one, must not be able to
        # smuggle in a velocity above MAX_VELOCITY or an offset wider than half
        # a step: scheduling assumes offsets stay inside their own step's
        # window, and a hit outside it is never found and never fires.
        steps = data.get("steps") or []
        offsets = data.get("offsets") or []
        for track in range(min(TRACK_COUNT, len(steps))):
            row = steps[track]
            for step in range(min(MAX_STEPS, len(row))):
                song.steps[track][step] = clamp(
                    _as_int(row[step], OFF), OFF, MAX_VELOCITY
                )
        for track in range(min(TRACK_COUNT, len(offsets))):
            row = offsets[track]
            for step in range(min(MAX_STEPS, len(row))):
                # Clamp into byte range before the assignment, not after: a
                # bytearray rejects anything outside 0-255 with a ValueError
                # at the moment of the store, so _reclamp_offsets below would
                # never get the chance to run.
                song.offsets[track][step] = clamp(
                    _as_int(row[step], OFFSET_BIAS), 0, 255
                )
        song._reclamp_offsets()
        return song


def _clamp_volume(value):
    """A loudness multiplier from anything, bounded to what the mixer can take."""
    return clamp(
        _as_number(value, DEFAULT_TRACK_VOLUME), MIN_TRACK_VOLUME, MAX_TRACK_VOLUME
    )


def _as_int(value, default):
    """An integer from a decoded file, or the default if it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _as_number(value, default):
    """A float from a decoded file, or the default if it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_name(value):
    """A filename from a decoded file, or None if it is not one."""
    return value if isinstance(value, str) else None
