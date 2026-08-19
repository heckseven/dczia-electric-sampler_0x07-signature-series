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
