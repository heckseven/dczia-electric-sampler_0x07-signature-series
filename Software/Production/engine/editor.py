"""Changing one number with one knob.

Several settings are a single value inside a range - a pattern length, a
tempo - and they all want the same behaviour: the knob moves it, one button
keeps it, the other puts it back. Doing that once here means the settings
screen has one way of editing a number rather than one per row.

Edits are live. The value is applied as the knob turns rather than on
confirm, because a pattern length is something you judge by listening, and a
number you cannot hear until you commit is a number you have to commit twice
to choose. Cancel therefore has real work to do: it restores what was there
when the editor opened.

Pure logic, so what the knob does to the value is tested without a knob.
"""

from engine.util import accelerated, clamp


class Editor:
    """One number being changed."""

    def __init__(self, label, value, minimum, maximum, apply=None, formatter=None):
        self.label = label
        self.minimum = minimum
        self.maximum = maximum
        self.value = clamp(value, minimum, maximum)
        # Remembered after clamping, not before. A song saved when the limit
        # was higher opens on a value outside the range, and cancelling has
        # to put back something legal rather than the number that was
        # rejected on the way in.
        self.original = self.value
        self._apply = apply
        self._format = formatter
        # Set once the value has been put back, so a caller cannot cancel
        # twice and restore over a later edit.
        self.cancelled = False

    @property
    def text(self):
        if self._format is not None:
            return self._format(self.value)
        return str(self.value)

    @property
    def at_minimum(self):
        return self.value <= self.minimum

    @property
    def at_maximum(self):
        return self.value >= self.maximum

    def turn(self, delta, elapsed_ms=None):
        """Move the value, scaled by how fast the knob is turning.

        The same acceleration the volume uses: creep it for a small change,
        spin it to cross the range. A length runs to 64 steps, which is a
        long way at one per detent.
        """
        if not delta:
            return self.value
        return self.set(self.value + accelerated(delta, elapsed_ms))

    def set(self, value):
        value = clamp(int(value), self.minimum, self.maximum)
        if value != self.value:
            self.value = value
            self._push()
        return self.value

    def commit(self):
        """Keep the value. Returns it, so a caller can report what was set."""
        return self.value

    def cancel(self):
        """Put back what was there when this opened."""
        if self.cancelled:
            return self.value
        self.cancelled = True
        self.value = self.original
        self._push()
        return self.value

    def _push(self):
        if self._apply is not None:
            self._apply(self.value)
