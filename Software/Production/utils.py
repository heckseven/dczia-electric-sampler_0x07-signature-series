import screen

from setup import (
    display,
)


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
    total_lines = 3
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


def show_menu(menu, highlight, shift):
    """Shows the menu on the screen.

    Drawn as three independent lines with a ">" cursor rather than a filled
    highlight bar. The bar was a 128x10 block switching on and off with every
    scroll step, which is the largest possible change on this panel and the
    noisiest thing the display can do to the audio.
    """
    global _menu_screen
    total_lines = 3
    if _menu_screen is None:
        _menu_screen = screen.TextScreen(lines=total_lines)
        _menu_screen.attach(display)

    for line in range(total_lines):
        index = shift + line
        try:
            pretty = menu[index]["pretty"]
        except IndexError:
            pretty = ""
        cursor = ">" if pretty and highlight == line + 1 else " "
        _menu_screen.set_line(line, "%s%s" % (cursor, pretty))
