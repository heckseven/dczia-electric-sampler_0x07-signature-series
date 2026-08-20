"""Turning stored micro-timing into the tick a hit actually fires on.

Recording stores what was played: each hit keeps the signed tick offset from
its own grid line. Quantise strength is then applied on the way out rather than
when recording, so the knob stays reversible - dial it back and the original
feel returns, because the performance was never overwritten.

    strength 0.0   plays exactly as performed
    strength 1.0   snaps every hit to the grid
    strength 0.5   pulls each hit halfway toward it

Finding which hits are due on a given tick is cheap because offsets are clamped
to half a step in Song. A hit belonging to step S can only fall inside
[S*ticks - ticks/2, S*ticks + ticks/2), so for any tick exactly one step per
track is even a candidate: the nearest grid line. That makes the per-tick cost
eight checks rather than a scan of the pattern, which matters at 24 PPQN.

Negative offsets on step 0 need no special handling. Working modulo the pattern
length puts them at the end of the loop, which is where a hit played slightly
ahead of the downbeat belongs.

This module imports nothing from CircuitPython.
"""

MIN_STRENGTH = 0.0
MAX_STRENGTH = 1.0
DEFAULT_STRENGTH = 1.0  # fully quantised until the player asks otherwise
STRENGTH_STEP = 0.05


def clamp_strength(value):
    if value < MIN_STRENGTH:
        return MIN_STRENGTH
    if value > MAX_STRENGTH:
        return MAX_STRENGTH
    return value


def effective_offset(offset, strength):
    """How far from the grid a hit plays, after strength is applied."""
    if offset == 0 or strength >= MAX_STRENGTH:
        return 0
    remaining = offset * (1.0 - clamp_strength(strength))
    # Round to nearest, symmetrically about zero. Ticks are the finest thing
    # that can be played, so a residue below half a tick is genuinely on the
    # grid. One consequence worth knowing: at fine divisions an offset spans
    # only a couple of ticks, so strength has correspondingly few
    # distinguishable settings there, and more at coarse ones.
    if remaining > 0:
        return int(remaining + 0.5)
    return -int(-remaining + 0.5)


def pattern_ticks(length, ticks_per_step):
    return length * ticks_per_step


def nearest_step(tick, ticks_per_step, length):
    """The only step whose half-step window can contain this tick."""
    return int((tick + ticks_per_step // 2) // ticks_per_step) % length


def step_fires_at(song, track, step, strength, length=None):
    """Absolute tick within this track's pattern where the hit sounds.

    Within *this track's* pattern: tracks have their own lengths, so they
    wrap at their own points and a tick means a different position in each.
    """
    ticks = song.ticks_per_step
    if length is None:
        length = song.track_length(track)
    total = pattern_ticks(length, ticks)
    offset = effective_offset(song.offset(track, step), strength)
    return (step * ticks + offset) % total


def hits_due(song, tick, strength, include_muted=False):
    """Every (track, step, velocity) that should sound on this tick.

    `strength` is the global setting; a track carrying its own override uses
    that instead, so one track can swing while the rest stay straight.
    """
    ticks = song.ticks_per_step
    due = []
    # Each track is walked on its own clock. There is no shared pattern
    # position once lengths differ: at the same tick, a sixteen-step track
    # and a twelve-step one are at different points in their own bars, and
    # the pair only agree again after forty-eight steps. That drift is the
    # feature, so nothing here may hoist the length out of the loop.
    for track in range(len(song.steps)):
        length = song.track_length(track)
        total = pattern_ticks(length, ticks)
        position = tick % total
        step = nearest_step(position, ticks, length)
        velocity = song.steps[track][step]
        if velocity == 0:
            continue
        if song.muted[track] and not include_muted:
            continue
        track_strength = song.strength_for(track, strength)
        if step_fires_at(song, track, step, track_strength, length) == position:
            due.append((track, step, velocity))
    return due


def max_offset_for(ticks_per_step):
    """Matches Song.max_offset: strictly inside the half-step window."""
    return (ticks_per_step - 1) // 2


def quantize_hit(tick, ticks_per_step, length):
    """Snap a recorded hit to a step, returning (step, offset from it).

    Used when a live pad hit is captured. The offset is what makes the
    strength knob meaningful later; the step is where the pad lights.

    The offset is clamped to the same range Song will store, so capture and
    storage cannot disagree. A hit landing exactly on the boundary between two
    steps is therefore nudged at most one tick toward its own grid line, which
    is the correct trade: the alternative is an offset Song would silently
    clamp anyway, leaving playback a tick from where capture claimed it was.
    """
    total = pattern_ticks(length, ticks_per_step)
    position = tick % total
    step = nearest_step(position, ticks_per_step, length)
    offset = position - step * ticks_per_step
    # A hit just before the loop point belongs to step 0 of the next pass, not
    # to the last step of this one, so express the offset as the short way
    # round rather than nearly a whole pattern.
    if offset > total // 2:
        offset -= total
    elif offset < -(total // 2):
        offset += total
    limit = max_offset_for(ticks_per_step)
    if offset > limit:
        offset = limit
    elif offset < -limit:
        offset = -limit
    return step, offset
