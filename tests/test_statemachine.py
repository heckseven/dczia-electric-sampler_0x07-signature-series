"""Tests for the state machine, and for when each screen gets built.

Building every state at boot is what left the sampler playing with about 9 KB
of heap free, and an RP2040 out of heap under a real-time load fails as a hard
fault with no traceback, an OSError out of the audio path, or USB endpoints
dying while the loop keeps running. All three were seen on the badge.
"""

import sys

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
from statemachine import STATES, StateMachine


@pytest.fixture
def machine():
    return StateMachine()


def test_nothing_is_built_before_it_is_needed(machine):
    assert machine.states == {}


def test_entering_a_state_builds_only_that_one(machine):
    machine.go_to_state("settings")
    assert list(machine.states) == ["settings"]


def test_playing_a_pattern_never_builds_the_screens_it_does_not_use(machine):
    """The path the player takes: boot, menu, sampler.

    Flashy, MIDI and HID are 38 KB between them - the animation library and
    the HID library above all - carried for screens nobody is looking at.
    """
    machine.go_to_state("startup")
    machine.go_to_state("settings")
    machine.go_to_state("sampler")
    for unused in ("flashy", "midi_controller", "hid"):
        assert unused not in machine.states


def test_the_heavy_modules_are_not_imported_either(machine):
    """Deferring construction is not enough: the import is most of the cost.

    FlashyState is a small object on top of a large library, so importing it
    and never building it would save almost nothing.
    """
    for module in ("FlashyState", "HIDState", "MIDIState"):
        sys.modules.pop(module, None)
    machine.go_to_state("settings")
    machine.go_to_state("sampler")
    for module in ("FlashyState", "HIDState", "MIDIState"):
        assert module not in sys.modules, "%s was imported unnecessarily" % module


def test_a_state_is_built_once_and_kept(machine):
    machine.go_to_state("settings")
    first = machine.states["settings"]
    machine.go_to_state("sampler")
    machine.go_to_state("settings")
    assert machine.states["settings"] is first


def test_visiting_a_heavy_screen_still_works(machine):
    machine.go_to_state("settings")
    machine.go_to_state("flashy")
    assert machine.state.name == "flashy"


def test_leaving_a_state_records_it(machine):
    machine.go_to_state("settings")
    machine.go_to_state("sampler")
    assert machine.last_state == "settings"


def test_every_registered_state_can_actually_be_built(machine):
    """A name in STATES whose module or class is misspelt is a crash on entry.

    Nothing catches that until a player presses the button that goes there,
    because the import is deferred - which is the point of the table, and
    also what makes this test worth having.
    """
    for name in STATES:
        assert machine.state_for(name).name == name


def test_updating_before_entering_anything_is_harmless(machine):
    machine.update()
