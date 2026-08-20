import screen

from setup import (
    display,
)

# How many menu entries fit on the 128x32 panel at a 10px line height.
MENU_LINES = 3


def neoindex(key_number):
    """Which pixel sits at a given key.

    Established by lighting each pixel on the badge one at a time and
    writing down what was underneath it. The boards cannot settle this:
    the LEDs are on the back copper of the front panel and the switches on
    the front copper of the main board, so whether the two agree about left
    and right depends on how the panel is mounted, which no file records.
    Two earlier versions of this table were derived from the geometry and
    both were wrong.

    Observed, walking the strip from one end:

        pixel 0    Function
        pixel 1    Play
        pixels 2-5 upper pad row, right to left   (pads 4, 3, 2, 1)
        pixels 6-9 lower pad row, left to right   (pads 5, 6, 7, 8)

    So the strip starts at the buttons, doubles back along the upper row,
    and runs forward along the lower one. The key matrix numbers things the
    other way round - both pad rows left to right, then Play, then Function
    - which is why nothing about this table looks tidy.
    """
    mapping = [5, 4, 3, 2, 6, 7, 8, 9, 1, 0]
    try:
        neopixel_index = mapping[key_number]
    except IndexError:
        neopixel_index = 0
    return neopixel_index


def selector_calcs(menu, highlight, shift, last_position, position):
    list_length = len(menu)
    total_lines = MENU_LINES
    if position < last_position:
        if highlight > 1:
            highlight -= 1
        else:
            if shift > 0:
                shift -= 1
    else:
        if highlight < total_lines:
            highlight += 1
        else:
            if shift + total_lines < list_length:
                shift += 1
    return (highlight, shift)


# One screen reused across calls. Rebuilding the scene graph time would
# resend the whole display, which pops the amplifier; see screen.py.
_menu_screen = None


def _ensure_menu_screen(lines=MENU_LINES):
    global _menu_screen
    if _menu_screen is None:
        _menu_screen = screen.shared(display, lines=lines)
    return _menu_screen


def attach_menu():
    """Put the menu back on the display, and return it.

    Attaching belongs to entering the menu, not to building its screen. Other
    states show groups of their own, so coming back from one leaves the
    display pointed somewhere else; a menu screen that only attached itself
    once, when it was first created, would then update labels nobody could
    see. Doing it per entry rather than per redraw also keeps it off the
    scroll path, where a full-screen reattach is exactly the noise the
    per-line drawing exists to avoid.
    """
    menu_screen = _ensure_menu_screen()
    menu_screen.attach()
    # Deliberately not drawn here. Callers set their own text immediately
    # after this and flush it themselves; drawing now would only push the
    # previous state's text to the panel for one frame.
    return menu_screen


def show_menu(menu, highlight, shift):
    """Set the menu text. Returns the screen, which the caller flushes.

    Drawn as lines of text with a ">" cursor rather than a filled highlight
    bar. The bar was a 128x10 block switching on and off with every scroll
    step, which is the largest possible change this panel can make.

    Nothing is pushed to the display here. Scrolling competes with the audio,
    so drawing is paced by the caller's flush; see screen.py.
    """
    menu_screen = _ensure_menu_screen()

    for line in range(MENU_LINES):
        index = shift + line
        try:
            pretty = menu[index]["pretty"]
        except IndexError:
            pretty = ""
        cursor = ">" if pretty and highlight == line + 1 else " "
        menu_screen.set_line(line, "%s%s" % (cursor, pretty))
    return menu_screen
