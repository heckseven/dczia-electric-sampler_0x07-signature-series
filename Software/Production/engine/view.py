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
STEP_ON = (0, 0, 255)  # a recorded note, dimmed by its velocity
PLAYHEAD = (255, 255, 255)  # the step sounding right now
OUT_OF_PATTERN = (12, 0, 0)  # past the loop point: present but not playing

# LIVE view
TRACK_LOADED = (0, 40, 40)  # a pad with a sample behind it
TRACK_EMPTY = OFF
TRACK_FLASH = (255, 120, 0)  # struck just now
TRACK_SELECTED = (0, 120, 120)  # the track the encoders are editing
TRACK_MUTED = (40, 0, 0)

# Indicators, on the two pixels at the Play and Function buttons
STOPPED = OFF
PLAYING = (0, 120, 0)
RECORDING = (255, 0, 0)
ARMED = (120, 0, 0)  # blinks: waiting for the first pad hit
MODE_LIVE = (0, 0, 60)
MODE_SEQ = (0, 60, 0)
CLOCK_EXTERNAL = (60, 0, 60)
CLOCK_FLYWHEEL = (30, 0, 30)  # blinks: external latched, no pulses arriving

PLAY_PIXEL = 8
FUNCTION_PIXEL = 9

LIVE = "live"
SEQ = "seq"


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
    """The eight pad colours while editing a track's steps."""
    colors = []
    for slot in range(STEPS_PER_PAGE):
        step = page * STEPS_PER_PAGE + slot
        if step >= song.track_length(track):
            colors.append(OUT_OF_PATTERN)
            continue
        if step == playhead:
            colors.append(PLAYHEAD)
            continue
        velocity = song.velocity(track, step)
        colors.append(scale(STEP_ON, velocity) if velocity else OFF)
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


def pads(song, mode, loaded, track=0, page=0, playhead=None, flashing=()):
    if mode == SEQ:
        return seq_pads(song, track, page, playhead)
    return live_pads(song, loaded, selected=track, flashing=flashing)


def play_indicator(transport, blink=False):
    """The pixel at the Play button: what the transport is doing."""
    if transport.armed:
        # Blinking says "waiting for you", which a solid colour cannot.
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
