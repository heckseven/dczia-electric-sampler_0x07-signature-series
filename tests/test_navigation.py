"""Tests for menu navigation, the startup sequence and LED animation modes.

These states are pure UI: they read encoder positions and key events, redraw the
display, and hand control to the state machine. They had no coverage at all, so
a change to scrolling or to a menu's wiring could not be caught before flashing
a badge.
"""

import os

import pytest

import circuitpython_stubs
from conftest import FakeMachine
from conftest import PRODUCTION_DIR
import screen as screen_module
import setup
from FlashyState import FlashyState
from HIDState import HIDState
from MIDIState import MIDIState
from StartupState import StartupState

TOTAL_LINES = 3


@pytest.fixture(autouse=True)
def clean_input():
    setup.keys.events.clear()
    setup.select_enc.position = 0
    setup.volume_enc.position = 0
    yield
    setup.keys.events.clear()


def press(key_number):
    setup.keys.events.post(circuitpython_stubs.Event(key_number, pressed=True))


# --- every state a transition names actually exists ------------------------


def test_every_state_a_screen_moves_to_is_registered():
    """Guards against a transition naming a state that does not exist.

    go_to_state takes a string, so a renamed state is only caught when a
    player presses the button that goes there. The names are read out of the
    state classes themselves rather than written out again here, so renaming
    one without updating statemachine.STATES fails.

    main.py cannot be imported - its event loop runs at module scope - so the
    same classes it registers are imported directly.
    """
    from HIDState import HIDState
    from MIDIState import MIDIState
    from SamplerState import SamplerState
    from SettingsState import SettingsState
    from StartupState import StartupState
    from statemachine import STATES

    known = {
        state.name
        for state in (
            FlashyState(),
            MIDIState(),
            HIDState(),
            SamplerState(),
            SettingsState(),
            StartupState(),
        )
    }
    assert set(STATES) == known


# --- FlashyState ----------------------------------------------------------


def test_flashy_selects_an_animation_when_scrolled():
    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)
    before = state._animation
    setup.select_enc.position += 1
    state.update(machine)
    assert state._animation is not before


def test_every_animation_on_the_menu_can_be_selected():
    """A row naming an animation that does not exist would draw nothing."""
    from engine import animation

    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)
    for index in range(len(animation.NAMES)):
        setup.select_enc.position += 1
        state.update(machine)
        assert state._animation is animation.by_name(state.menu.selected.label)


def test_flashy_returns_to_the_settings_tree_on_keypress():
    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)
    press(0)
    state.update(machine)
    assert machine.transitions == ["settings"]


def test_leaving_flashy_turns_the_strip_off():
    """It is decoration; leaving it lit would read as the sampler's state."""
    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)
    state.update(machine)
    state.exit(machine)
    assert setup.neopixels[0] == (0, 0, 0)


# --- StartupState ---------------------------------------------------------


def test_startup_ends_in_the_sampler():
    machine = FakeMachine()
    state = StartupState()
    state.enter(machine)
    for _ in range(3000):
        state.update(machine)
        if machine.transitions:
            break
    assert machine.transitions == ["sampler"]


def test_startup_is_skipped_by_a_keypress():
    machine = FakeMachine()
    state = StartupState()
    state.enter(machine)
    press(0)
    for _ in range(20):
        state.update(machine)
        if machine.transitions:
            break
    assert machine.transitions == ["sampler"]


def test_startup_never_addresses_a_pixel_it_does_not_have():
    """Stage two walks the strip by index; it must stay inside it."""
    machine = FakeMachine()
    state = StartupState()
    state.enter(machine)
    for _ in range(3000):
        state.update(machine)
        if machine.transitions:
            break


def test_startup_clears_the_strip_on_entry():
    machine = FakeMachine()
    setup.neopixels.fill((9, 9, 9))
    StartupState().enter(machine)
    assert setup.neopixels[0] == (0, 0, 0)


# --- the menu draws without a full-width highlight bar --------------------


def test_a_menu_marks_the_selection_with_a_cursor():
    """A filled highlight bar is a 128x10 block switching on every scroll step.

    That is the largest change this panel can make and the noisiest thing the
    display can do to the audio, so the selection is marked with a character.
    """
    state = FlashyState()
    state.enter(FakeMachine())
    rows = state.menu.rendered()
    assert rows[0].startswith(">"), rows
    assert rows[1].startswith(" "), rows


# --- getting the display back ---------------------------------------------
#
# Every state points the display at a group of its own, so a menu does not
# keep it just because it had it once. A menu that updates labels while the
# display is showing somebody else's group looks frozen: scrolling and backing
# out both appear to do nothing, because the drawing is real but invisible.


def test_entering_a_menu_puts_it_on_the_display():
    state = FlashyState()
    state.enter(FakeMachine())
    assert setup.display.shown is screen_module.shared(setup.display).group


def test_a_menu_takes_the_display_back_from_another_state():
    """Returning from the sampler, or any state that shows its own group."""
    import displayio

    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)  # the menu owns the display

    somebody_else = displayio.Group()
    setup.display.show(somebody_else)  # another state takes it
    assert setup.display.shown is somebody_else

    state.enter(machine)  # and we come back
    assert setup.display.shown is screen_module.shared(setup.display).group


def test_scrolling_does_not_reattach_the_display():
    """Reattaching is a full-screen redraw, the loudest thing on this panel.

    It belongs on state entry, which happens once, not on the scroll path,
    which happens for every detent.
    """
    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)
    attached = setup.display.shown

    calls = []
    real = setup.display.show
    setup.display.show = lambda group: (calls.append(group), real(group))[1]
    try:
        for _ in range(10):
            setup.select_enc.position += 1
            state.update(machine)
    finally:
        setup.display.show = real
    assert calls == [], "scrolling reattached the display"
    assert setup.display.shown is attached


def test_the_menu_still_redraws_late_in_the_tick_period():
    """ticks_ms counts from an unspecified point and wraps at 2**29.

    A zero sentinel for "not drawn yet" is not neutral: once the counter is
    more than half a period past it, the wrap-safe difference goes negative
    and the redraw is never due again, so scrolling stops updating the
    screen for as long as that state is on.
    """
    from SettingsState import SettingsState

    ticks = circuitpython_stubs.ticks
    before = ticks.value
    try:
        ticks.value = (1 << 28) + 5000  # past half the tick period
        machine = FakeMachine()
        state = SettingsState()
        state.enter(machine)
        shared = screen_module.shared(setup.display)
        shared.set_line(1, "stale")

        setup.select_enc.position += 1
        for _ in range(500):
            ticks.value += 10
            state.update(machine)

        assert shared.drawn(1) != "stale", "the settings screen never redrew"
    finally:
        ticks.value = before


# --- every screen draws through screen.py --------------------------------
#
# The point of the shared screen is that no state hand-rolls its own drawing.
# A state that shows text without taking the display, or takes it without
# drawing, looks frozen - which is exactly how the Flashy menu broke when
# show_menu stopped drawing on the caller's behalf.


@pytest.mark.parametrize(
    "state_class", [FlashyState, MIDIState, HIDState, StartupState]
)
def test_entering_a_state_puts_its_text_on_the_display(state_class):
    state = state_class()
    state.enter(FakeMachine())
    shared = screen_module.shared(setup.display)
    assert setup.display.shown is shared.group, (
        "%s did not take the display" % state_class.__name__
    )


@pytest.mark.parametrize("state_class", [FlashyState, MIDIState, HIDState])
def test_entering_a_state_leaves_nothing_waiting_to_be_drawn(state_class):
    """Entering should show the screen, not reveal it a line per pass."""
    state = state_class()
    state.enter(FakeMachine())
    assert screen_module.shared(setup.display).pending == 0


def test_the_flashy_menu_redraws_when_scrolled():
    """It stopped drawing entirely when show_menu became queue-only."""
    machine = FakeMachine()
    flashy = FlashyState()
    flashy.enter(machine)
    shared = screen_module.shared(setup.display)
    before = [shared.drawn(i) for i in range(len(shared))]

    setup.select_enc.position += 1
    for _ in range(200):
        flashy.update(machine)

    after = [shared.drawn(i) for i in range(len(shared))]
    assert after != before, "scrolling Flashy changed nothing on screen"


def test_only_one_text_screen_is_ever_built():
    """A scene graph and glyph cache per state, for one panel, is waste."""
    assert screen_module.shared(setup.display) is screen_module.shared(setup.display)


# --- the badge recovering on its own --------------------------------------
#
# It has hung with the CPU inside a peripheral driver: still powered, still
# enumerated, frozen on the last thing drawn, and unreachable by Ctrl-C. The
# reset button is not reachable in a rack, so restarting itself is the only
# recovery the player actually has.


def test_the_main_loop_runs_a_watchdog():
    source = open(os.path.join(PRODUCTION_DIR, "main.py")).read()
    assert "guard.arm()" in source
    assert "guard.feed()" in source, "an armed watchdog that is never fed resets"


def test_the_watchdog_is_fed_inside_the_loop():
    """Feeding outside the loop arms a reset that always fires."""
    source = open(os.path.join(PRODUCTION_DIR, "main.py")).read()
    loop = source.index("while True:")
    assert source.index("guard.feed()", loop) > loop


def test_the_sampler_draws_through_the_shared_screen():
    """A second screen means two sets of tile grids on one font bitmap.

    Which one exists depends on how the sampler was reached: entering it
    from the menu builds both, entering it directly builds only its own.
    That is the difference between a badge that runs and one that does not,
    and it is not a difference any screen should be able to create.
    """
    from SamplerState import SamplerState

    state = SamplerState()
    assert state._screen is screen_module.shared(setup.display)


def test_only_one_screen_exists_however_states_are_reached():
    from SamplerState import SamplerState

    screens = set()
    for state_class in (FlashyState, MIDIState, HIDState, StartupState):
        state = state_class()
        state.enter(FakeMachine())
        screens.add(id(setup.display.shown))
    sampler = SamplerState()
    sampler.enter(FakeMachine())
    screens.add(id(setup.display.shown))
    assert len(screens) == 1, "%d different groups were shown" % len(screens)
