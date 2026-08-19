"""Drawing text without disturbing the audio.

Updating the display audibly pops the amplifier, and the size of the pop
tracks the area displayio resends - not how many pixels visibly change.
Measured on the badge with a pattern playing:

    raw I2C traffic, nothing drawn ........ silent
    a 4x4 region, at any update rate ...... silent
    one character inside a full-width label  very noisy
    the whole screen ...................... very noisy

The third line is the trap. Changing a single character looks like a tiny
update, but displayio marks the whole label dirty and resends its entire
area, so a label spanning the screen makes every change a full-screen
update. Brightness made no difference, and neither did the NeoPixels, so
this is the lever that matters.

The fix is to give each line its own label. Changing one line then resends
one line's worth of area, and lines that did not change send nothing at
all. Lines are also skipped when their text is unchanged, so a redraw that
changes nothing costs nothing.
"""

import terminalio
from adafruit_display_text import label

import displayio

LINE_HEIGHT = 10
LEFT = 2
BASELINE = 6


class TextScreen:
    """A few independent lines of text, each redrawn on its own."""

    def __init__(self, lines=3, line_height=LINE_HEIGHT):
        self.group = displayio.Group()
        self._labels = []
        self._text = []
        for index in range(lines):
            item = label.Label(
                terminalio.FONT,
                text="",
                x=LEFT,
                y=BASELINE + index * line_height,
            )
            self.group.append(item)
            self._labels.append(item)
            self._text.append("")

    def __len__(self):
        return len(self._labels)

    def set_line(self, index, text):
        """Update one line. Returns True if it actually changed."""
        if text == self._text[index]:
            return False
        self._text[index] = text
        self._labels[index].text = text
        return True

    def set_lines(self, texts):
        """Update several lines, touching only those that differ."""
        changed = False
        for index in range(min(len(self._labels), len(texts))):
            if self.set_line(index, texts[index]):
                changed = True
        return changed

    def line(self, index):
        return self._text[index]

    def attach(self, display):
        """Put this screen on the display. Call once, not per update."""
        display.show(self.group)

    def clear(self):
        for index in range(len(self._labels)):
            self.set_line(index, "")
