"""The state machine, and when each screen is built.

Separated from main.py so it can be tested: main.py ends in a loop that never
returns, which makes it unimportable.
"""

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
#     MenuState      1.2 KB
#
# Playing a pattern needs Startup, Menu and Sampler. The other three are 38 KB
# the badge was carrying for screens the player is not looking at.
STATES = {
    "startup": ("StartupState", "StartupState"),
    "menu": ("MenuState", "MenuState"),
    "sampler": ("SamplerState", "SamplerState"),
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
            module = __import__(module_name)
            state = getattr(module, class_name)()
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
