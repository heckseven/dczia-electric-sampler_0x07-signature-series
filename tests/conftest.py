"""Make Software/Production importable by installing CircuitPython stubs first.

setup.py initialises hardware at import time, so the stubs have to be in
sys.modules before anything under Software/Production is imported. Doing it at
module scope here guarantees that, because pytest imports conftest before
collecting any test module.
"""

import os
import sys

import circuitpython_stubs

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
# Overridable so the suite can be pointed at another checkout - useful for
# confirming that a regression test really does fail against the code it
# describes, rather than passing everywhere and proving nothing.
PRODUCTION_DIR = os.environ.get(
    "DCZIA_PRODUCTION_DIR", os.path.join(REPO_ROOT, "Software", "Production")
)

circuitpython_stubs.install()

# The firmware imports its own modules flatly (`from setup import display`), so
# Software/Production has to be on the path as if it were the filesystem root.
if PRODUCTION_DIR not in sys.path:
    sys.path.insert(0, PRODUCTION_DIR)


class FakeMachine:
    """A state machine for tests, which records where it was sent.

    It builds states the way the real one does, because StartupState warms
    the settings screen through `state_for` - a fake without it would only
    ever exercise the import half of warming and never the card half.
    """

    def __init__(self):
        self.animation = None
        self.last_state = None
        self.transitions = []
        self.state = None
        self._built = {}

    def go_to_state(self, name):
        self.transitions.append(name)

    def state_for(self, name):
        import statemachine

        state = self._built.get(name)
        if state is None:
            module_name, class_name = statemachine.STATES[name]
            state = getattr(__import__(module_name), class_name)()
            self._built[name] = state
        return state
