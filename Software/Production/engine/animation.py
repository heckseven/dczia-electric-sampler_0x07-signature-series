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

from engine.clock import PPQN, ticks_diff

PIXEL_COUNT = 10

# The pads in reading order - pad 1 first, pad 8 last - as pixel indices.
# This has to agree with utils.neoindex, and a test asserts that it does.
PADS = (5, 4, 3, 2, 6, 7, 8, 9)

# The two pad rows, each left to right.
UPPER = PADS[:4]
LOWER = PADS[4:]

# The two button pixels. They are a row of their own, sitting above the pads
# and aligned with the left edge of them - so the panel is three rows, the
# top one only two wide:
#
#     [Fn][Play]
#     [p1][p2][p3][p4]
#     [p5][p6][p7][p8]
#
# That shape is why COLUMNS is ragged: the two left-hand columns are three
# pixels tall and the two right-hand ones are two.
FUNCTION_PIXEL = 0
PLAY_PIXEL = 1
INDICATORS = (FUNCTION_PIXEL, PLAY_PIXEL)
BUTTONS = INDICATORS

# Left to right across the panel. The first two columns have a button on top
# of them; the other two are pads only. What a sweep travels along.
COLUMNS = (
    (FUNCTION_PIXEL, UPPER[0], LOWER[0]),
    (PLAY_PIXEL, UPPER[1], LOWER[1]),
    (UPPER[2], LOWER[2]),
    (UPPER[3], LOWER[3]),
)

# What anything that travels goes round: every pixel, in strip order, which
# is already a serpentine over the panel - along the buttons, back along the
# top pad row, forward along the bottom one.
#
# Ten does not divide the sixteen sixteenths of a bar, so anything stepping
# on the sixteenth would come back to the top only every five bars. Travelling
# animations are therefore positioned by where they are *in* the bar rather
# than counted in sixteenths: one lap per bar, whatever the path length. The
# steps no longer land on the beat grid, which is the price of the buttons
# joining in, and the lap staying locked to the bar is worth more.
PATH = tuple(range(PIXEL_COUNT))

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


def _travelling_position(tick):
    """Where a travelling animation is, as an index into PATH.

    Taken from the position in the bar rather than counted in sixteenths, so
    a lap is exactly one bar however many pixels the path has.
    """
    return int(bar_phase(tick) * len(PATH)) % len(PATH)


def chase(tick, brightness=1.0):
    """One lit pixel, once round the whole panel every bar."""
    colors = _blank()
    colors[PATH[_travelling_position(tick)]] = dim(
        wheel(bar_phase(tick) * 255), brightness
    )
    return colors


def comet(tick, brightness=1.0, tail=4):
    """A chase with a tail behind it, so the direction reads at speed."""
    head = _travelling_position(tick)
    hue = bar_phase(tick) * 255
    colors = _blank()
    for step in range(tail):
        colors[PATH[(head - step) % len(PATH)]] = dim(
            wheel(hue), brightness * (1.0 - step / float(tail))
        )
    return colors


def sweep(tick, brightness=1.0):
    """A column of light crossing the panel and coming back, once a bar.

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
    return [color] * PIXEL_COUNT


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


# The most time one step of the free-running clock will account for. A pass
# that took longer than this was a stall - a collection, a card read - and
# advancing the animation by all of it would show as a lurch rather than as
# the motion it stands in for.
MAX_STEP_MS = 250


class Timebase:
    """A tick that keeps counting when the sequencer's does not.

    The clock only advances while the transport is running, but the lights
    should still move when it is not: a panel that goes dead the moment you
    press stop looks broken.

    The naive version of this - deriving a tick from the millisecond counter
    and the tempo - does not work, and hardware is what showed it. Absolute
    elapsed time multiplied by tempo is a different number line from the
    clock's own tick, so handing over between them jumps: measured on the
    badge, an animation tick of 10765 became 120 the moment the transport
    started, which is a jump to an unrelated phase. The same expression also
    jumps whenever the tempo changes, because every past millisecond is
    suddenly worth more ticks than it was.

    So it accumulates instead. While the clock runs its tick is authoritative
    and is simply followed; when it stops, counting carries on from wherever
    that left it, at whatever tempo was last known. Both handovers are
    continuous, in both directions.
    """

    def __init__(self):
        self._tick = 0.0
        self._last_ms = None

    def step(self, now_ms, bpm, clock_tick=None):
        """Advance to `now_ms` and return the tick to draw.

        `clock_tick` is the sequencer's own tick while it is running, and None
        when it is not.
        """
        if clock_tick is not None:
            self._tick = float(clock_tick)
            self._last_ms = now_ms
            return clock_tick
        if self._last_ms is None:
            self._last_ms = now_ms
            return int(self._tick)
        # Wrap-safe: the millisecond counter rolls over at 2**29, and a plain
        # subtraction across that point is a large negative number.
        elapsed = ticks_diff(now_ms, self._last_ms)
        self._last_ms = now_ms
        if elapsed <= 0 or bpm <= 0:
            return int(self._tick)
        if elapsed > MAX_STEP_MS:
            elapsed = MAX_STEP_MS
        self._tick += elapsed * bpm * PPQN / 60000.0
        return int(self._tick)
