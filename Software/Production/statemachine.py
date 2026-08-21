"""The state machine, and when each screen is built.

Separated from main.py so it can be tested: main.py ends in a loop that never
returns, which makes it unimportable.
"""

import guard

# Which module and class each state lives in, so a state can be built the
# first time it is asked for rather than at boot.
#
# This is a memory decision, not a tidiness one. Measured on the badge, with
# every state built up front the sampler plays with about 9 KB of heap left,
# and the badge fails the way an RP2040 fails when it runs out under a
# real-time load: a hard fault with no traceback, or an OSError out of the
# audio path, or the USB endpoints dying while the loop keeps going.
#
# Importing is most of the cost, so this defers that too:
#
#     FlashyState   23.5 KB   pulls in five animations from adafruit_led_animation
#     SamplerState  10.3 KB
#     HIDState       9.6 KB   pulls in adafruit_hid
#     MIDIState      5.1 KB
#     StartupState   2.3 KB
#     SettingsState  1.4 KB
#
# Playing a pattern needs Startup, Sampler and Settings. The other three are
# 38 KB the badge was carrying for screens the player is not looking at.
#
# Settings is in that list rather than deferred with the rest because its
# modules take 1.3 seconds to compile off the card, and the audio buffer
# holds 32 milliseconds - opening it mid-pattern would be forty buffers of
# silence. StartupState imports it while the banner is still up, where the
# time costs nothing. What stays deferred inside it is the card: the rows
# listing songs, kits and samples are built when they are opened, so a
# directory listing never lands on the path that opens the screen.
STATES = {
    "startup": ("StartupState", "StartupState"),
    "sampler": ("SamplerState", "SamplerState"),
    "settings": ("SettingsState", "SettingsState"),
    "flashy": ("FlashyState", "FlashyState"),
    "midi_controller": ("MIDIState", "MIDIState"),
    "hid": ("HIDState", "HIDState"),
}


class StateMachine(object):
    def __init__(self):
        self.state = None
        self.states = {}
        self.animation = None
        self.last_state = None

    def state_for(self, name):
        """Build a state the first time it is needed, then keep it.

        Importing the module is deferred as well, because that is where most
        of the memory goes: FlashyState is 1.2 KB of state object on top of
        23.5 KB of animation library.
        """
        state = self.states.get(name)
        if state is None:
            module_name, class_name = STATES[name]
            # Compiling a screen off the card measured 250 to 1500 ms, against
            # a two second watchdog that nothing feeds while this blocks. The
            # margin was under a second, and card timings vary by a factor of
            # three between runs - so this held it off rather than gambling.
            # A reset here looks exactly like a crash: the badge reboots with
            # no traceback, because a reset is not an exception.
            state = guard.slowly(lambda: getattr(__import__(module_name), class_name)())
            self.states[name] = state
        return state

    def go_to_state(self, state_name):
        if self.state:
            self.state.exit(self)
            self.last_state = self.state.name
        self.state = self.state_for(state_name)
        self.state.enter(self)

    def update(self):
        if self.state:
            self.state.update(self)
