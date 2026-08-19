"""Entry point.

The loop does two things: tick the sequencer engine, and update whatever state
is on screen. That split is the whole point of the rework. The engine is a
module-level singleton rather than a state, so the beat keeps running while you
sit in the menu, change an LED animation or browse samples - and states become
pure UI that render and handle input.

The engine's tick never blocks. The previous sequencer spun in
`while ticks_ms() < deadline: pass` for the length of every step, which is why
input was dropped and the display could not be touched during playback.
"""

from FlashyState import FlashyState
from HIDState import HIDState
from MenuState import MenuState
from MIDIState import MIDIState
from SamplerState import SamplerState
from sequencer import engine
from StartupState import StartupState


class StateMachine(object):
    def __init__(self):
        self.state = None
        self.states = {}
        self.animation = None
        self.last_state = None

    def add_state(self, state):
        self.states[state.name] = state

    def go_to_state(self, state_name):
        if self.state:
            self.state.exit(self)
            self.last_state = self.state.name
        self.state = self.states[state_name]
        self.state.enter(self)

    def update(self):
        if self.state:
            self.state.update(self)


machine = StateMachine()
machine.add_state(StartupState())
machine.add_state(FlashyState())
machine.add_state(MIDIState())
machine.add_state(HIDState())
machine.add_state(MenuState())
machine.add_state(SamplerState())
machine.go_to_state("startup")

while True:
    # The beat runs regardless of what is on screen.
    engine.tick()
    machine.update()
