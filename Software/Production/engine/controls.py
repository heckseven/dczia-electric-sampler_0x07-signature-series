"""Turning key presses into intentions.

The badge has twelve keys and needs far more than twelve actions, so Function
and Play double as held modifiers. That creates an ambiguity: a press of
Function might be the start of a chord or might be a tap meaning "switch mode",
and nothing can tell which until it is released.

The resolution is a consumed flag. A modifier press arms it; anything pressed
while it is held consumes it; on release, an unconsumed modifier fires its own
tap action. Nothing needs a hold timeout, so no gesture waits on a timer.

    Function tap ............. switch between LIVE and SEQ
    Function + pad ........... select that track
    Function + Play .......... arm or disarm recording
    Function + Volume click .. clear the selected track
    Play tap ................. start or stop the transport
    Play + pad (SEQ) ......... jump to that page
    Play + pad (LIVE) ........ erase that track as the playhead passes
    pad ...................... trigger it, or toggle a step in SEQ
    Select click ............. back to the menu
    Volume click ............. mute the selected track

The cost, accepted deliberately: because Play can start a chord, its transport
action fires on release rather than press, which adds the length of a tap to
starting the beat by hand.

This module imports nothing from CircuitPython.
"""

# Key numbers, as the keypad matrix in setup.py reports them.
PAD_FIRST = 0
PAD_LAST = 7
PLAY = 8
FUNCTION = 9
SELECT = 10
VOLUME = 11

# Actions. The value carried, where there is one, is in the second slot.
PAD = "pad"  # a pad was struck
PAD_RELEASE = "pad_release"  # ...and let go, which ends a velocity edit
SELECT_TRACK = "select_track"
PAGE = "page"
ERASE = "erase"
TOGGLE_MODE = "toggle_mode"
TOGGLE_TRANSPORT = "toggle_transport"
ARM_RECORD = "arm_record"
CLEAR_TRACK = "clear_track"
MUTE = "mute"
BACK = "back"

# Pad meaning depends on which view is showing.
LIVE = "live"
SEQ = "seq"


def is_pad(key_number):
    return PAD_FIRST <= key_number <= PAD_LAST


class Controls:
    """Interprets key events. Feed it presses and releases, read actions back."""

    def __init__(self, mode=LIVE):
        self.mode = mode
        self._function_held = False
        self._play_held = False
        self._function_consumed = False
        self._play_consumed = False
        self._pads_held = set()

    # --- queries ----------------------------------------------------------

    @property
    def function_held(self):
        return self._function_held

    @property
    def play_held(self):
        return self._play_held

    def pad_is_held(self, pad):
        return pad in self._pads_held

    @property
    def held_pads(self):
        return sorted(self._pads_held)

    # --- events -----------------------------------------------------------

    def press(self, key_number):
        """Returns a list of (action, value) for this press."""
        if key_number == FUNCTION:
            self._function_held = True
            self._function_consumed = False
            if self._play_held:
                # Function+Play, arrived the other way round. Consume both so
                # neither fires its tap action on release: the chord means one
                # thing regardless of which key was pressed first.
                self._play_consumed = True
                self._function_consumed = True
                return [(ARM_RECORD, None)]
            return []

        if key_number == PLAY:
            self._play_held = True
            self._play_consumed = False
            if self._function_held:
                self._function_consumed = True
                self._play_consumed = True
                return [(ARM_RECORD, None)]
            return []

        if is_pad(key_number):
            self._pads_held.add(key_number)
            if self._function_held:
                self._function_consumed = True
                return [(SELECT_TRACK, key_number)]
            if self._play_held:
                self._play_consumed = True
                if self.mode == SEQ:
                    return [(PAGE, key_number)]
                return [(ERASE, key_number)]
            return [(PAD, key_number)]

        if key_number == SELECT:
            return [(BACK, None)]

        if key_number == VOLUME:
            if self._function_held:
                self._function_consumed = True
                return [(CLEAR_TRACK, None)]
            return [(MUTE, None)]

        return []

    def release(self, key_number):
        """Returns a list of (action, value) for this release."""
        if key_number == FUNCTION:
            self._function_held = False
            if not self._function_consumed:
                return [(TOGGLE_MODE, None)]
            return []

        if key_number == PLAY:
            self._play_held = False
            if not self._play_consumed:
                return [(TOGGLE_TRANSPORT, None)]
            return []

        if is_pad(key_number):
            self._pads_held.discard(key_number)
            return [(PAD_RELEASE, key_number)]

        return []

    def handle(self, key_number, pressed):
        return self.press(key_number) if pressed else self.release(key_number)

    # --- encoder context --------------------------------------------------

    def select_turn_target(self):
        """What the Select encoder adjusts right now."""
        if self._function_held:
            return "length"
        if self._play_held:
            return "swing"
        if self._pads_held:
            return "step_velocity"
        return "bpm"

    def volume_turn_target(self):
        """What the Volume encoder adjusts right now."""
        if self._function_held:
            return "division"
        if self._play_held:
            return "quantize"
        return "volume"

    def set_mode(self, mode):
        self.mode = mode
        return self.mode
