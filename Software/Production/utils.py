"""What the badge's hardware layout is, where no file records it.

Once a whole menu system as well. That went when the settings tree replaced
it - the tree lives in engine/menu.py, is tested without a display, and
scrolls without the highlight bar that used to repaint a 128x10 block on
every detent. What is left is the one thing that cannot be derived from
anything and had to be measured.
"""


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
