"""What the LEDs and the screen should show.

Rendering is separated from driving the hardware so it can be tested: these
functions take the engine's state and return colours and strings, and the UI
layer merely pushes those at the NeoPixels and the display. A wrong colour is
then a failing test rather than something noticed while squinting at a badge.

The pixel layout is fixed by the board. Reading the front panel against the
main board by X coordinate, D101-D108 sit above and below the eight pads and
D109 and D110 sit at the Play and Function buttons - so the strip is eight pad
lights plus two indicators, and utils.neoindex maps a key to its own pixel.

Colours are deliberately few and far apart. The panel is diffused and viewed at
an angle, so hue carries the meaning and brightness carries velocity.
"""

from engine.song import MAX_VELOCITY, STEPS_PER_PAGE, TRACK_COUNT

OFF = (0, 0, 0)

# SEQ view
#
# Two levels of one colour, not a palette. The pads used to paint every
# recorded step in blue dimmed by its velocity, which on a diffused panel at
# arm's length reads as a wall rather than as information. Showing only the
# playhead was the other extreme and went too far: with the transport stopped
# the panel went dark, so there was no way to see what you had just toggled -
# and toggling steps is the whole of sequence editing.
#
# So: a recorded step is dim, the step sounding now is bright, and the same
# white says both. One glance answers "what is in this bar" and "where am I"
# without the two competing.
STEP_ON = (28, 28, 28)  # a recorded note, waiting its turn
# Magenta rather than white, and at full scale on both its channels. It
# cannot be made brighter than this here: the strip is built at
# brightness=0.1 in setup.py, so that is the knob for the whole panel. Note
# also that magenta reads dimmer than a white of the same numbers because
# green carries most of what the eye counts as brightness, which is why the
# recorded steps are pulled down a little to keep the contrast.
PLAYHEAD = (255, 0, 255)  # the step sounding right now
OUT_OF_PATTERN = (12, 0, 0)  # past the loop point: present but not playing

# Which of the eight tracks the encoders are editing, shown while Function is
# held down - which is also the chord that selects one, so the panel answers
# the question the gesture asks.
TRACK_PICK = (255, 255, 255)

# The dim tier of the same white. One rule across every overlay: bright is
# where you are, dim is something that exists but is not current, off is
# nothing there. Same value as a recorded step, so the panel has one idea of
# "present but not now".
PRESENT = STEP_ON

# LIVE view
#
# The same two tiers as everywhere else, in magenta: bright is the track the
# encoders are on, dim is a track with a sample behind it, dark is empty. A
# hit is white, which is the one colour nothing else on this panel uses for
# more than an instant - so a pattern playing draws itself across the pads
# as it goes.
TRACK_LOADED = (28, 0, 28)  # a pad with a sample behind it
TRACK_EMPTY = OFF
TRACK_FLASH = (255, 255, 255)  # sounding right now, struck or sequenced
TRACK_SELECTED = (255, 0, 255)  # the track the encoders are editing
TRACK_MUTED = (40, 0, 0)

# Indicators, on the two pixels at the Play and Function buttons.
#
# Play says what the transport is doing: magenta stopped, green running, red
# recording or waiting to. Function says which view is showing, in the same
# two colours the pads use for it - magenta for LIVE, white for SEQ - so the
# button and the panel under it agree.
STOPPED = (255, 0, 255)
PLAYING = (0, 120, 0)
RECORDING = (255, 0, 0)
ARMED = (255, 0, 0)  # blinks: armed, but the sequencer has not started
MODE_LIVE = (255, 0, 255)
MODE_SEQ = (255, 255, 255)
CLOCK_EXTERNAL = (60, 0, 60)
CLOCK_FLYWHEEL = (30, 0, 30)  # blinks: external latched, no pulses arriving

PLAY_PIXEL = 8
FUNCTION_PIXEL = 9

LIVE = "live"
SEQ = "seq"


def centred(text, columns):
    """Pad on the left so a line sits in the middle of its row.

    Only the left pad is added. The display uses a fixed-width font and
    trailing spaces draw as nothing, so padding both sides would spend
    characters to change nothing - and screen.set_line truncates to the row
    width anyway, which a right pad would run into.

    str.center would do this, and is deliberately not used: CircuitPython
    only builds it when CIRCUITPY_FULL_BUILD is set, so it is always present
    in tests on desktop Python and may not be on the badge. That is the worst
    way to find out.
    """
    room = columns - len(text)
    if room <= 0:
        return text
    return " " * (room // 2) + text


def scale(color, velocity):
    """Dim a colour by a step's velocity, keeping it visible at the low end."""
    if velocity <= 0:
        return OFF
    factor = 0.25 + 0.75 * (float(velocity) / MAX_VELOCITY)
    return (
        int(color[0] * factor),
        int(color[1] * factor),
        int(color[2] * factor),
    )


def seq_pads(song, track, page, playhead=None):
    """The eight pad colours while editing a track's steps.

    Bright where the playhead is, dim where a step is recorded, dark
    otherwise. The playhead wins when it is standing on a recorded step: the
    question "where am I" is the more urgent of the two, and the step is
    audible at that moment anyway.
    """
    colors = []
    for slot in range(STEPS_PER_PAGE):
        step = page * STEPS_PER_PAGE + slot
        if step == playhead:
            colors.append(PLAYHEAD)
        elif step < song.track_length(track) and song.velocity(track, step):
            colors.append(STEP_ON)
        else:
            colors.append(OFF)
    return colors


def track_pads(selected, loaded=()):
    """The eight pad colours while Function is held: which track is current.

    Bright where the cursor is, dim where a track has a sample to play, dark
    where there is nothing - so the same glance answers "which one am I on"
    and "which ones are worth going to".
    """
    colors = []
    for track in range(TRACK_COUNT):
        if track == selected:
            colors.append(TRACK_PICK)
        elif track < len(loaded) and loaded[track]:
            colors.append(PRESENT)
        else:
            colors.append(OFF)
    return colors


def page_pads(song, track, page):
    """The eight pad colours while Play is held: which page is showing.

    Bright for the page in front of you, dim for the pages this track
    actually has, dark past the end of the pattern. Turning Select while Play
    is held changes the length, so the dim run grows and shrinks under your
    fingers as you do it.
    """
    pages = song.page_count_for(track)
    colors = []
    for slot in range(STEPS_PER_PAGE):
        if slot == page:
            colors.append(TRACK_PICK)
        elif slot < pages:
            colors.append(PRESENT)
        else:
            colors.append(OFF)
    return colors


def live_pads(song, loaded, selected=None, flashing=()):
    """The eight pad colours while playing pads by hand."""
    colors = []
    for track in range(TRACK_COUNT):
        if track in flashing:
            colors.append(TRACK_FLASH)
        elif song.muted[track]:
            colors.append(TRACK_MUTED)
        elif not loaded[track]:
            colors.append(TRACK_EMPTY)
        elif track == selected:
            colors.append(TRACK_SELECTED)
        else:
            colors.append(TRACK_LOADED)
    return colors


def pads(
    song,
    mode,
    loaded,
    track=0,
    page=0,
    playhead=None,
    flashing=(),
    function_held=False,
    play_held=False,
):
    """What the eight pads show.

    A held modifier takes the panel over, because while one is down the pads
    mean what it says they mean rather than what the view says. Function asks
    "which track", Play asks "which page"; Function wins if somehow both are
    down, matching the chord order in controls.
    """
    if function_held:
        return track_pads(track, loaded)
    if play_held:
        return page_pads(song, track, page)
    if mode == SEQ:
        return seq_pads(song, track, page, playhead)
    return live_pads(song, loaded, selected=track, flashing=flashing)


def play_indicator(transport, blink=False):
    """The pixel at the Play button: what the transport is doing.

    Red covers both recording and waiting to record, because in both the
    next thing you play is being kept. The two are still told apart by the
    blink: armed pulses because it is waiting on you, recording sits solid
    because it is not.
    """
    if transport.armed:
        return ARMED if blink else OFF
    if transport.recording:
        return RECORDING
    if transport.playing:
        return PLAYING
    return STOPPED


def function_indicator(mode, clock=None, blink=False, now=None):
    """The pixel at the Function button: which view, and where the clock is.

    `now` is needed to tell a live external clock from one that has stopped
    sending pulses, because flywheeling is a question about elapsed time
    rather than a stored flag.
    """
    if clock is not None and clock.source == "ext":
        if now is not None and clock.is_flywheeling(now):
            return CLOCK_FLYWHEEL if blink else OFF
        return CLOCK_EXTERNAL
    return MODE_SEQ if mode == SEQ else MODE_LIVE


def status_line(song, mode, track, page, transport, clock):
    """The top line of the display: where you are."""
    if mode == SEQ:
        where = "T%d P%d/%d" % (track + 1, page + 1, song.page_count_for(track))
    else:
        where = "LIVE T%d" % (track + 1)
    marks = []
    if transport.recording:
        marks.append("REC")
    elif transport.armed:
        marks.append("ARM")
    if clock is not None and clock.source == "ext":
        marks.append("EXT")
    tail = " ".join(marks)
    return ("%-12s %s" % (where, tail)).rstrip()


def detail_line(song, clock, volume_percent=None):
    """The second line: the numbers you change most.

    Volume is where the knob is, not the level it produces. The level is a
    decibel curve, so the quiet half of the dial is all values below a
    hundredth - a display of those would read 0 for most of its useful
    travel and tell the player nothing.
    """
    # A single length is a lie once tracks differ, so say so rather than
    # pick one: "L16" means every track, "L*" means they vary and the
    # per-track number is on the SEQ page for the track being edited.
    length = "L%d" % song.length if song.uniform_length else "L*"
    line = "%d %s %s" % (int(clock.bpm), song.division_name, length)
    if volume_percent is not None:
        line += " V%d" % volume_percent
    return line


def step_row(song, track, page, playhead=None):
    """A compact picture of one page: * is a note, o the playhead, . empty."""
    row = []
    for slot in range(STEPS_PER_PAGE):
        step = page * STEPS_PER_PAGE + slot
        if step >= song.track_length(track):
            row.append(" ")
        elif step == playhead:
            row.append("o")
        elif song.velocity(track, step):
            row.append("*")
        else:
            row.append(".")
    return "".join(row)
