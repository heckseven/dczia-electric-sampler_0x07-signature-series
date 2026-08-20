"""Spelling a name with one encoder and two buttons.

There is no keyboard. What there is: a knob that turns, a button that means
yes, and a button that means no. So a name is spelled one letter at a time -
turn to pick a letter, press yes to keep it and move on, press no to rub the
last one out.

Finishing needs a third gesture and there is no third button, so the
alphabet carries an end marker as its first entry. Turning back past A
reaches it, which puts "done" one detent from where the hand already is
after choosing a letter rather than at the far end of thirty-eight of them.

Pure logic: no keys, no screen. What comes out is the text so far, the
letter currently under the knob, and whether the player has finished.
"""

# The end marker lives at index 0 so a single turn backwards from A reaches
# it. Lower case is left out deliberately - it doubles the turning for names
# shown on a 21-character screen in a single case anyway.
#
# It is a control character rather than a glyph because the badge's font is
# ASCII only: an arrow or a tick would draw as a blank box, which is worse
# than useless for the one row that has to say "press here to finish". The
# view spells it out as a word instead - see DONE_LABEL.
DONE = "\x01"
DONE_LABEL = "OK"
ALPHABET = DONE + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ "

MAX_LENGTH = 16


class NameEntry:
    """A name being spelled out, one letter at a time."""

    def __init__(self, initial="", max_length=MAX_LENGTH):
        self.max_length = max_length
        self._letters = [c for c in (initial or "")[:max_length] if c in ALPHABET]
        self._index = 1  # start on A, one turn from done
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
    def at_end_marker(self):
        return self._index == 0

    @property
    def letter_label(self):
        """What to draw for the letter under the knob.

        The end marker has no glyph in an ASCII font, so it is spelled out.
        A space is drawn as an underscore for the same reason: an invisible
        character under a cursor looks like nothing is happening.
        """
        if self.at_end_marker:
            return DONE_LABEL
        if self.letter == " ":
            return "_"
        return self.letter

    @property
    def preview(self):
        """The name with the letter being chosen shown at the end."""
        if self.at_end_marker:
            return self.text
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
        list is short and has real ends, here it is a loop the hand spins
        and the end marker should be reachable from Z as well as from A.
        """
        if self._finished or self._cancelled:
            return self.letter
        self._index = (self._index + delta) % len(ALPHABET)
        return self.letter

    def accept(self):
        """Yes. Keeps the letter and moves on, or finishes on the end marker.

        Returns True when the name is complete.
        """
        if self._finished or self._cancelled:
            return self._finished
        if self.at_end_marker:
            self._finished = True
            return True
        if self.full:
            # Nowhere to put it. Silently dropping the letter would look
            # like a broken button, so treat a full name as done.
            self._finished = True
            return True
        self._letters.append(self.letter)
        self._index = 1
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
