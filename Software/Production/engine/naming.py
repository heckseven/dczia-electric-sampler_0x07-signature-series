"""Spelling a name with one encoder and three buttons.

There is no keyboard. What there is: a knob that turns and clicks, a button
that means yes, and a button that means no. So a name is spelled one letter
at a time - turn to pick a letter, click the knob to keep it and move on,
press Function to rub the last one out, press Play when the name is right.

Finishing used to need a marker at the front of the alphabet, reached by
turning back past A, because there was no third gesture to spare. There is
now: the encoder's own click sets a letter, which frees Play to mean done.
That is worth the change on its own - "press the thing you are already
turning" is a shorter sentence than "turn to a hidden entry before A" - and
it also lines the screen up with every other one on the badge, where Play is
yes and Function is no.

Pure logic: no keys, no screen. What comes out is the text so far, the
letter currently under the knob, and whether the player has finished.
"""

# Lower case is left out deliberately: it doubles the turning, for names
# shown on a 21-character screen in a single case anyway.
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ "

MAX_LENGTH = 16


class NameEntry:
    """A name being spelled out, one letter at a time."""

    def __init__(self, initial="", max_length=MAX_LENGTH):
        self.max_length = max_length
        self._letters = [c for c in (initial or "")[:max_length] if c in ALPHABET]
        self._index = 0  # start on A
        self._finished = False
        self._cancelled = False

    # --- what the player sees --------------------------------------------

    @property
    def text(self):
        """The name so far, without the letter still being chosen."""
        return "".join(self._letters)

    @property
    def letter(self):
        """The letter currently under the knob."""
        return ALPHABET[self._index]

    @property
    def letter_label(self):
        """What to draw for the letter under the knob.

        A space is drawn as an underscore: an invisible character under a
        cursor looks like nothing is happening.
        """
        if self.letter == " ":
            return "_"
        return self.letter

    @property
    def preview(self):
        """The name with the letter being chosen shown at the end."""
        return self.text + self.letter

    @property
    def finished(self):
        return self._finished

    @property
    def cancelled(self):
        return self._cancelled

    @property
    def full(self):
        return len(self._letters) >= self.max_length

    # --- what the player does --------------------------------------------

    def turn(self, delta):
        """Move through the alphabet. Wraps, because it is a ring of letters.

        Wrapping is right here and wrong in the settings menu: there the
        list is short and has real ends, here it is a loop the hand spins,
        and reaching Z from A should not mean winding through the digits.
        """
        if self._finished or self._cancelled:
            return self.letter
        self._index = (self._index + delta) % len(ALPHABET)
        return self.letter

    def finish(self):
        """Play. The name is right; stop here.

        Returns True, so a caller can treat it the same way it treats accept
        reporting that the name is complete.
        """
        if not self._cancelled:
            self._finished = True
        return self._finished

    def accept(self):
        """The encoder's click. Keeps the letter and moves on.

        Returns True when the name is complete, which now only happens if
        the caller asked to finish - kept as the return value so the two
        gestures read the same way at the call site.
        """
        if self._finished or self._cancelled:
            return self._finished
        if self.full:
            # Nowhere to put it. Play still finishes and Function still
            # rubs out, so the name is not stuck - and the preview visibly
            # stops growing, which says why.
            return False
        self._letters.append(self.letter)
        self._index = 0
        return False

    def backspace(self):
        """No. Rubs out the last letter, or cancels if there is nothing left.

        Returns True while there was still something to rub out.
        """
        if self._finished or self._cancelled:
            return False
        if self._letters:
            self._letters.pop()
            self._index = 1
            return True
        self._cancelled = True
        return False

    def result(self):
        """The finished name, or None if it was cancelled or is still going."""
        if not self._finished:
            return None
        name = self.text.strip()
        return name or None
