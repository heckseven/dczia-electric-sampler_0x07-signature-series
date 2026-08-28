"""Turning key presses into intentions.

The badge has twelve keys and needs far more than twelve actions, so Function
and Play double as held modifiers. That creates an ambiguity: a press of
Function might be the start of a chord or might be a tap meaning "switch mode",
and nothing can tell which until it is released.

There are two ways a modifier's own action is suppressed, and both are needed.

A consumed flag covers the chords: a modifier press arms it, anything pressed
while it is held consumes it, and on release an unconsumed modifier fires its
tap action. That handles Function+pad and the rest without any timing at all.

It does not handle holding a modifier by itself. Holding Function now shows a
track picker and holding Play shows the pages, so a hold alone is a real
gesture with nothing to consume it - and releasing it used to toggle the mode
or the transport, which mid-set is the worst kind of surprise. So duration is
the second test: a press released inside HOLD_MS is a tap and does the key's
own job, and anything longer is a hold and does nothing on release.

This module was written with no timing in it at all, and that was worth
keeping until a hold alone had to mean something. `now` is therefore optional
everywhere: leave it out and the timing test is skipped, which is what the
pure-logic tests of the chord behaviour do.

    HOLD_MS is the whole of the tap/hold definition and the only place to
    change it. Everything that needs to know whether a key is being held
    asks `is_hold`, so raising or lowering it moves every gesture at once.

    Function tap ............. switch between LIVE and SEQ
    Function + pad ........... select that track
    Function + Play .......... arm or disarm recording
    Function + Volume click .. clear the selected track
    Function + Select turn ... the selected track's pitch
    Function + Volume turn ... the selected track's volume
    Play + Select turn ....... pattern length
    Play + Volume turn ....... quantize strength
    pad + Select turn ........ pitch: of that step in SEQ, that track in LIVE
    pad + Volume turn ........ that step's velocity in SEQ, track volume in LIVE
    Play tap ................. start or stop the transport
    Play + pad (SEQ) ......... jump to that page
    Play + pad (LIVE) ........ erase that track as the playhead passes
    pad ...................... trigger it, or toggle a step in SEQ
    Select click ............. back to the menu
    Volume click ............. mute the selected track

The cost, accepted deliberately: because Play can start a chord, its transport
action fires on release rather than press, which adds the length of a tap to
starting the beat by hand.

This module imports nothing from CircuitPython: `now` arrives as a plain
millisecond count from whoever has a clock.
"""

from engine.clock import ticks_diff

# How long a press has to last before it stops being a tap.
#
# The whole definition of the distinction, in one number, so it can be tuned
# by changing this alone. 250 ms is a deliberate compromise: comfortably
# longer than a deliberate tap, which measures around 80 ms, and short enough
# that reaching for a chord does not feel like waiting. Everything that cares
# asks is_hold rather than comparing timestamps itself.
HOLD_MS = 250

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
SETTINGS = "settings"

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
        # When each key went down, for the tap/hold test. Keys the caller
        # gave no timestamp for are simply absent, and then every press is a
        # tap - which is what the pure-logic tests rely on.
        self._pressed_at = {}

    # --- queries ----------------------------------------------------------

    @property
    def function_held(self):
        return self._function_held

    @property
    def play_held(self):
        return self._play_held

    def pad_is_held(self, pad):
        return pad in self._pads_held

    def is_hold(self, key_number, now=None):
        """Whether this key has been down long enough to be a hold.

        The single definition of tap versus hold. False when the key is not
        down, when no timestamp was recorded for it, or when the caller has
        no clock to offer - all of which mean "treat it as a tap".
        """
        if now is None:
            return False
        started = self._pressed_at.get(key_number)
        if started is None:
            return False
        return ticks_diff(now, started) >= HOLD_MS

    def held_long(self, key_number, now=None):
        """Whether a modifier is down and past the threshold.

        What the display asks to decide when to put a legend up, so the
        legend appears at exactly the moment the gesture stops being a tap.
        """
        if key_number == FUNCTION and not self._function_held:
            return False
        if key_number == PLAY and not self._play_held:
            return False
        if is_pad(key_number) and key_number not in self._pads_held:
            return False
        return self.is_hold(key_number, now)

    def any_held_long(self, now=None):
        """Whether anything held has been down long enough to be a hold.

        Asked twice on every pass of the main loop, so it must not allocate.
        The obvious spelling - iterating `(FUNCTION, PLAY) + tuple(held_pads)`
        - built a sorted list, a tuple and a concatenation per call, measured
        at 96 bytes a pass and about sixty per cent of everything the loop
        allocated. That is what was driving the collector, and a collection
        is 26 ms against a 32 ms audio buffer.

        The empty case is checked before iterating because it is the usual
        one, and even an iterator over an empty set is an object.

        `now` is optional here as everywhere else in this module: without a
        clock nothing can have been held long, which is what held_long already
        answers.
        """
        if self.held_long(FUNCTION, now) or self.held_long(PLAY, now):
            return True
        if not self._pads_held:
            return False
        for key in self._pads_held:
            if self.held_long(key, now):
                return True
        return False

    @property
    def held_pads(self):
        return sorted(self._pads_held)

    # --- events -----------------------------------------------------------

    def press(self, key_number, now=None):
        """Returns a list of (action, value) for this press."""
        if now is not None:
            self._pressed_at[key_number] = now
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
            # The click that opens the settings tree. It used to back out to a
            # menu; the sampler is now where the badge starts, so there is
            # nothing above it to back out to.
            return [(SETTINGS, None)]

        if key_number == VOLUME:
            if self._function_held:
                self._function_consumed = True
                return [(CLEAR_TRACK, None)]
            return [(MUTE, None)]

        return []

    def release(self, key_number, now=None):
        """Returns a list of (action, value) for this release."""
        was_hold = self.is_hold(key_number, now)
        self._pressed_at.pop(key_number, None)

        if key_number == FUNCTION:
            self._function_held = False
            if not self._function_consumed and not was_hold:
                return [(TOGGLE_MODE, None)]
            return []

        if key_number == PLAY:
            self._play_held = False
            if not self._play_consumed and not was_hold:
                return [(TOGGLE_TRANSPORT, None)]
            return []

        if is_pad(key_number):
            self._pads_held.discard(key_number)
            return [(PAD_RELEASE, key_number)]

        return []

    def handle(self, key_number, pressed, now=None):
        if pressed:
            return self.press(key_number, now)
        return self.release(key_number, now)

    # --- encoder context --------------------------------------------------

    # Holding something scopes the knobs to it. A pad is a step in SEQ and a
    # track in LIVE, Function is the selected track, Play is the whole
    # pattern - and Select and Volume then mean that thing's pitch and its
    # loudness. Play is the exception, because a pattern has no pitch: its
    # two properties are how long it is and how hard it is quantised.

    def select_turn_target(self):
        """What the Select encoder adjusts right now."""
        if self._function_held:
            return "track_pitch"
        if self._play_held:
            return "length"
        if self._pads_held:
            return "step_pitch" if self.mode == SEQ else "track_pitch_held"
        return "bpm"

    def volume_turn_target(self):
        """What the Volume encoder adjusts right now."""
        if self._function_held:
            return "track_volume"
        if self._play_held:
            return "quantize"
        if self._pads_held:
            return "step_velocity" if self.mode == SEQ else "track_volume_held"
        return "volume"

    def legend(self):
        """What the held modifier offers, one line per gesture.

        Returned rather than drawn so the wording is testable without a
        display, and so the caller decides when it is worth showing.
        """
        if self._function_held:
            return ("pad  track", "Sel  pitch", "Vol  volume")
        if self._play_held:
            return ("pad  page", "Sel  length", "Vol  quantize")
        if self._pads_held:
            if self.mode == SEQ:
                return ("Sel  step pitch", "Vol  step level", "")
            return ("Sel  pitch", "Vol  volume", "")
        return None

    def set_mode(self, mode):
        self.mode = mode
        return self.mode
