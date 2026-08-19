import screen

from setup import (
    display,
)

# How many menu entries fit on the 128x32 panel at a 10px line height.
MENU_LINES = 3


def neoindex(key_number):
    # Front board carries exactly 10 addressable LEDs (D101-D110).
    # D101-D108 sit above/below the 8 pads; D109 and D110 sit at the
    # Play and Function buttons, so keys 8 and 9 map to pixels 8 and 9.
    mapping = [4, 5, 6, 7, 3, 2, 1, 0, 8, 9]
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
        _menu_screen = screen.TextScreen(lines=lines)
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
    menu_screen.attach(display)
    return menu_screen


def show_menu(menu, highlight, shift):
    """Shows the menu on the screen.

    Drawn as three independent lines with a ">" cursor rather than a filled
    highlight bar. The bar was a 128x10 block switching on and off with every
    scroll step, which is the largest possible change on this panel and the
    noisiest thing the display can do to the audio.
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
