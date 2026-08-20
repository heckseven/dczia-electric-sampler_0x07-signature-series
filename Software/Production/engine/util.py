"""Small helpers shared across the engine.

clamp is called sixteen times between clock.py and song.py, and both had
their own copy before this module existed. The alternative to a third module
is for one of them to import from the other, and the only sensible direction
there is wrong: a song is built on a clock's divisions, so clock.py importing
song.py inverts the dependency to borrow five lines. A module of its own costs
a namespace dict and a sys.modules entry, which is the cheaper of the two.

Like the rest of engine/, this imports nothing from CircuitPython.
"""


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


# How a knob's speed becomes a bigger step.
#
# A rotary encoder reports detents, not speed, so turning it slowly and
# spinning it hard differ only in how close together the detents arrive.
# Reading that gap back gives the acceleration a hand expects: creep for a
# small correction, spin for a big move, without a modifier or a second
# control.
#
# Below FAST_MS between movements the step is multiplied by MAX_FACTOR;
# above SLOW_MS it is not multiplied at all, so fine adjustment stays
# possible. In between it scales smoothly.
FAST_MS = 20
SLOW_MS = 250
MAX_FACTOR = 8


def accelerated(steps, elapsed_ms):
    """Scale a knob movement by how quickly it is being turned.

    `steps` is the detents seen this time, signed. `elapsed_ms` is the gap
    since the knob last moved; None means it has been still, which is the
    slow case. The result keeps the sign and never rounds a real movement
    away to nothing.
    """
    if not steps:
        return 0
    if elapsed_ms is None or elapsed_ms >= SLOW_MS:
        return steps
    if elapsed_ms <= FAST_MS:
        factor = MAX_FACTOR
    else:
        span = float(SLOW_MS - FAST_MS)
        factor = 1.0 + (MAX_FACTOR - 1) * (SLOW_MS - elapsed_ms) / span
    scaled = int(steps * factor)
    if scaled == 0:
        # A movement the player made must never round away to no movement.
        return 1 if steps > 0 else -1
    return scaled
