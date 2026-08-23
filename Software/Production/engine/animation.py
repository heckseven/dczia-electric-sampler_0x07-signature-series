"""Lights that move with the beat.

The library animations this replaces could not see the music. They ran on a
wall clock, so a chase at 0.1 s a step drifted against a 120 BPM pattern and
matched nothing; at any other tempo it was simply unrelated. Everything here
is a function of the sequencer's tick instead, which means one thing worth
saying plainly: because engine/clock.py already latches to an external clock
and flywheels through gaps in it, these animations follow a drum machine
plugged into the sync input for free, without knowing that is what happened.

Pure logic - a tick goes in, ten colours come out - so what the strip shows
on the third sixteenth of the second bar is a test rather than something
judged by waving a badge about.

The geometry is measured, not derived. The LEDs are on the back copper of the
front panel and the switches on the front copper of the main board, so no file
records whether the two agree about left and right; the table in utils.neoindex
was established by lighting each pixel and writing down what was under it, and
two earlier versions derived from the boards were both wrong. What that table
says, read as a strip:

    pixel 0    Function
    pixel 1    Play
    pixel 2-5  upper pad row, RIGHT to left   (pads 4, 3, 2, 1)
    pixel 6-9  lower pad row, left to right   (pads 5, 6, 7, 8)

So the strip snakes: it starts at the buttons, runs back along the top row and
forward along the bottom one. An animation that walks the strip in index order
therefore already moves the way the eye expects. One that wants to travel left
to right has to be told the columns, which is what COLUMNS is for.
"""

from engine.clock import PPQN

PIXEL_COUNT = 10

# The pads in reading order - pad 1 first, pad 8 last - as pixel indices.
# This has to agree with utils.neoindex, and a test asserts that it does.
PADS = (5, 4, 3, 2, 6, 7, 8, 9)

# The two pad rows, each left to right.
UPPER = PADS[:4]
LOWER = PADS[4:]

# Left to right across the panel, each column being the pad above and the pad
# below it. What a sweep travels along.
COLUMNS = tuple((UPPER[column], LOWER[column]) for column in range(4))

# The two button pixels. Kept apart from the grid because they are physically
# apart from it, and an animation that treats all ten as one row looks wrong.
FUNCTION_PIXEL = 0
PLAY_PIXEL = 1
INDICATORS = (FUNCTION_PIXEL, PLAY_PIXEL)

# What anything that travels goes round: the eight pads, in reading order.
#
# The pads rather than all ten pixels, for two reasons. The buttons mean
# something - Play is lit when the transport is running - and a chase running
# over them reads as state rather than decoration. And eight divides the
# sixteen sixteenths of a bar, where ten does not: a lap over ten pixels comes
# back to the top every five bars, which is exactly the drift against the
# music this rework exists to remove.
PATH = PADS

TICKS_PER_BEAT = PPQN
BEATS_PER_BAR = 4
TICKS_PER_BAR = PPQN * BEATS_PER_BAR
TICKS_PER_SIXTEENTH = PPQN // 4
SIXTEENTHS_PER_BAR = TICKS_PER_BAR // TICKS_PER_SIXTEENTH

OFF = (0, 0, 0)


# --- time -----------------------------------------------------------------


def beat_phase(tick):
    """Where we are inside the current beat, 0.0 at the beat to just under 1."""
    return (tick % TICKS_PER_BEAT) / float(TICKS_PER_BEAT)


def bar_phase(tick):
    """Where we are inside the current bar of four."""
    return (tick % TICKS_PER_BAR) / float(TICKS_PER_BAR)


def beat_of_bar(tick):
    """Which beat of the bar, 0 to 3."""
    return (tick // TICKS_PER_BEAT) % BEATS_PER_BAR


def sixteenth(tick):
    """A count of sixteenth notes, always increasing."""
    return tick // TICKS_PER_SIXTEENTH


# --- colour ---------------------------------------------------------------


def wheel(position):
    """A colour from a hue 0-255, full saturation."""
    position = int(position) % 256
    if position < 85:
        return (255 - position * 3, position * 3, 0)
    if position < 170:
        position -= 85
        return (0, 255 - position * 3, position * 3)
    position -= 170
    return (position * 3, 0, 255 - position * 3)


def dim(color, level):
    """Scale a colour. `level` is 0.0 to 1.0 and is clamped."""
    if level <= 0:
        return OFF
    if level > 1.0:
        level = 1.0
    return (int(color[0] * level), int(color[1] * level), int(color[2] * level))


def _blank():
    return [OFF] * PIXEL_COUNT


# --- the animations -------------------------------------------------------
#
# Every one takes a tick and returns ten colours. Brightness is a separate
# argument rather than baked in, because the panel is diffused and what looks
# right on a bench is dazzling in a dark room.


def pulse(tick, brightness=1.0):
    """The whole strip hit on the beat, decaying until the next one.

    The plainest possible statement of the tempo, and the one to check a sync
    lead against: if this is not landing with the kick, nothing else will.
    """
    level = (1.0 - beat_phase(tick)) ** 2
    color = dim(wheel(beat_of_bar(tick) * 64), level * brightness)
    return [color] * PIXEL_COUNT


def _step_hue(tick):
    """A hue that changes on the sixteenth rather than continuously.

    Anything that steps should step in colour too. A dot that sits still
    while its colour slides is the one combination that looks like a fault.
    """
    return (sixteenth(tick) % SIXTEENTHS_PER_BAR) * (255.0 / SIXTEENTHS_PER_BAR)


def chase(tick, brightness=1.0):
    """One lit pad, stepping a place every sixteenth. Twice round a bar."""
    lit = sixteenth(tick) % len(PATH)
    colors = _blank()
    colors[PATH[lit]] = dim(wheel(_step_hue(tick)), brightness)
    return colors


def comet(tick, brightness=1.0, tail=4):
    """A chase with a tail behind it, so the direction reads at speed."""
    head = sixteenth(tick) % len(PATH)
    hue = _step_hue(tick)
    colors = _blank()
    for step in range(tail):
        colors[PATH[(head - step) % len(PATH)]] = dim(
            wheel(hue), brightness * (1.0 - step / float(tail))
        )
    return colors


def sweep(tick, brightness=1.0):
    """A column of light crossing the pads and coming back, once a bar.

    Travels in real space rather than along the strip, so it reads as a left
    to right movement rather than as the snake the wiring actually is.
    """
    phase = bar_phase(tick) * 2.0
    if phase > 1.0:
        phase = 2.0 - phase  # back the other way for the second half
    position = phase * (len(COLUMNS) - 1)
    colors = _blank()
    hue = beat_of_bar(tick) * 64
    for index, column in enumerate(COLUMNS):
        distance = abs(index - position)
        if distance >= 1.0:
            continue
        level = (1.0 - distance) * brightness
        for pixel in column:
            colors[pixel] = dim(wheel(hue), level)
    return colors


def rainbow(tick, brightness=1.0):
    """A hue that walks the strip and turns once a bar."""
    base = bar_phase(tick) * 255
    colors = _blank()
    for index in range(PIXEL_COUNT):
        colors[index] = dim(wheel(base + index * (255.0 / PIXEL_COUNT)), brightness)
    return colors


def sparkle(tick, brightness=1.0, count=3):
    """Pixels lit at random, redrawn every sixteenth.

    The randomness is a hash of the beat rather than random.random(), so the
    same bar looks the same twice - which makes it testable, and means a
    looping pattern gets a repeating light show rather than a fizz.
    """
    colors = _blank()
    # Within the bar, so a looping pattern gets a repeating light show.
    seed = sixteenth(tick) % SIXTEENTHS_PER_BAR
    for index in range(count):
        # A small integer hash. Deliberately not `random`: this has to be a
        # function of the beat, and importing random for it would also make
        # every test here a matter of seeding.
        value = (seed * 2654435761 + index * 40503) & 0xFFFFFFFF
        pixel = (value >> 8) % PIXEL_COUNT
        colors[pixel] = dim(wheel((value >> 16) & 0xFF), brightness)
    return colors


def heartbeat(tick, brightness=1.0):
    """Two quick hits a beat apart in the bar, like a pulse taken by hand.

    Quieter than the others. It is the one to leave running while doing
    something else on the badge.
    """
    phase = beat_phase(tick)
    beat = beat_of_bar(tick)
    if beat not in (0, 2):
        level = 0.0
    elif phase < 0.18:
        level = 1.0 - phase / 0.18
    elif 0.25 <= phase < 0.40:
        level = 0.6 * (1.0 - (phase - 0.25) / 0.15)
    else:
        level = 0.0
    color = dim((180, 0, 40), level * brightness)
    colors = _blank()
    for pixel in PADS:
        colors[pixel] = color
    return colors


def off(tick, brightness=1.0):
    """Nothing at all, for playing in the dark."""
    return _blank()


# Order is the order they appear on screen. Labels are short because the panel
# is 21 characters wide and the cursor and scroll marker take two of them.
ANIMATIONS = (
    ("Pulse", pulse),
    ("Chase", chase),
    ("Comet", comet),
    ("Sweep", sweep),
    ("Rainbow", rainbow),
    ("Sparkle", sparkle),
    ("Heartbeat", heartbeat),
    ("Off", off),
)

NAMES = tuple(label for label, _function in ANIMATIONS)


def by_name(name):
    """Look one up. Returns the pulse if the name is not known."""
    for label, function in ANIMATIONS:
        if label == name:
            return function
    return pulse


def free_running_tick(elapsed_ms, bpm):
    """A tick count for when the transport is stopped.

    The clock only advances while it is running, but the lights should still
    move when it is not - a badge whose panel goes dead the moment you press
    stop looks broken. This is the same timebase the clock would produce at
    that tempo, so starting the transport does not visibly jump.
    """
    if bpm <= 0:
        return 0
    return int(elapsed_ms * bpm * PPQN / 60000.0)
