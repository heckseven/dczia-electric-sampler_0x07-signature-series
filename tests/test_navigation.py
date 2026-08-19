"""Tests for menu navigation, the startup sequence and LED animation modes.

These states are pure UI: they read encoder positions and key events, redraw the
display, and hand control to the state machine. They had no coverage at all, so
a change to scrolling or to a menu's wiring could not be caught before flashing
a badge.
"""

import os

import pytest

import circuitpython_stubs
from conftest import PRODUCTION_DIR
import screen as screen_module
import setup
from FlashyState import FlashyState
from HIDState import HIDState
from MenuState import MenuState
from MIDIState import MIDIState
from StartupState import StartupState
from utils import selector_calcs, show_menu

TOTAL_LINES = 3


class FakeMachine:
    def __init__(self):
        self.animation = None
        self.last_state = None
        self.transitions = []

    def go_to_state(self, name):
        self.transitions.append(name)


@pytest.fixture(autouse=True)
def clean_input():
    setup.keys.events.clear()
    setup.select_enc.position = 0
    setup.volume_enc.position = 0
    yield
    setup.keys.events.clear()


def press(key_number):
    setup.keys.events.post(circuitpython_stubs.Event(key_number, pressed=True))


# --- selector_calcs -------------------------------------------------------


def test_selector_scrolls_down_within_the_visible_window():
    highlight, shift = selector_calcs(["a"] * 6, 1, 0, 0, 1)
    assert (highlight, shift) == (2, 0)


def test_selector_shifts_the_window_once_it_reaches_the_bottom():
    highlight, shift = selector_calcs(["a"] * 6, TOTAL_LINES, 0, 0, 1)
    assert (highlight, shift) == (TOTAL_LINES, 1)


def test_selector_stops_at_the_end_of_the_list():
    length = 4
    highlight, shift = selector_calcs(["a"] * length, TOTAL_LINES, 1, 0, 1)
    assert (highlight, shift) == (TOTAL_LINES, 1)


def test_selector_scrolls_up_and_stops_at_the_top():
    highlight, shift = selector_calcs(["a"] * 6, 1, 0, 1, 0)
    assert (highlight, shift) == (1, 0)


def test_selector_unshifts_before_moving_the_highlight_up():
    highlight, shift = selector_calcs(["a"] * 6, 1, 2, 1, 0)
    assert (highlight, shift) == (1, 1)


# --- show_menu ------------------------------------------------------------


def test_show_menu_renders_the_visible_window():
    menu = [{"pretty": "one"}, {"pretty": "two"}, {"pretty": "three"}]
    show_menu(menu, 1, 0)
    assert setup.display.shown is not None


def test_show_menu_tolerates_a_list_shorter_than_the_window():
    show_menu([{"pretty": "only"}], 1, 0)
    assert setup.display.shown is not None


def test_show_menu_handles_an_empty_menu():
    show_menu([], 1, 0)
    assert setup.display.shown is not None


# --- MenuState ------------------------------------------------------------


def test_menu_starts_an_animation_on_entry():
    machine = FakeMachine()
    MenuState().enter(machine)
    assert machine.animation is not None


def test_menu_reuses_an_existing_animation():
    machine = FakeMachine()
    machine.animation = "already running"
    MenuState().enter(machine)
    assert machine.animation == "already running"


def test_turning_the_encoder_moves_the_highlight():
    machine = FakeMachine()
    state = MenuState()
    state.enter(machine)
    before = state.highlight
    setup.select_enc.position = 1
    state.update(machine)
    assert state.highlight == before + 1


def test_selecting_the_first_entry_enters_flashy():
    machine = FakeMachine()
    state = MenuState()
    state.enter(machine)
    press(0)
    state.update(machine)
    assert machine.transitions == ["flashy"]


def test_every_menu_entry_names_a_registered_state():
    """Guards against a menu entry pointing at a state that does not exist.

    The names are taken from the state classes themselves rather than a list
    written out here, so renaming a state without updating the menu fails.
    main.py cannot be imported - its event loop runs at module scope - so the
    same classes it registers are imported directly.
    """
    from HIDState import HIDState
    from MIDIState import MIDIState
    from SamplerState import SamplerState
    from StartupState import StartupState

    known = {
        state.name
        for state in (
            FlashyState(),
            MenuState(),
            MIDIState(),
            HIDState(),
            SamplerState(),
            StartupState(),
        )
    }
    for item in MenuState.menu_items:
        assert item["name"] in known, item


def test_scrolling_past_the_window_shifts_the_list():
    machine = FakeMachine()
    state = MenuState()
    state.enter(machine)
    for step in range(1, 6):
        setup.select_enc.position = step
        state.update(machine)
    assert state.shift > 0
    index = state.highlight - 1 + state.shift
    assert index < len(MenuState.menu_items)


# --- FlashyState ----------------------------------------------------------


def test_flashy_selects_an_animation_when_scrolled():
    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)
    setup.select_enc.position = 1
    state.update(machine)
    assert machine.animation is not None


@pytest.mark.parametrize(
    "name",
    ["rainbow", "rainbow_chase", "rainbow_comet", "rainbow_sparkle", "sparkle_pulse"],
)
def test_every_flashy_animation_can_be_constructed(name):
    machine = FakeMachine()
    FlashyState().animation_selector(machine, name)
    assert machine.animation is not None


def test_flashy_menu_functions_are_all_selectable():
    machine = FakeMachine()
    state = FlashyState()
    for item in FlashyState.menu_items:
        state.animation_selector(machine, item["function"])
        assert machine.animation is not None, item


def test_flashy_returns_to_the_menu_on_keypress():
    machine = FakeMachine()
    state = FlashyState()
    state.enter(machine)
    press(0)
    state.update(machine)
    assert machine.transitions == ["menu"]


def test_animations_take_ownership_of_auto_write():
    """adafruit_led_animation sets auto_write False; firmware relies on it."""
    machine = FakeMachine()
    setup.neopixels.auto_write = True
    FlashyState().animation_selector(machine, "rainbow")
    assert setup.neopixels.auto_write is False


# --- StartupState ---------------------------------------------------------


def test_startup_advances_through_its_stages():
    machine = FakeMachine()
    state = StartupState()
    state.enter(machine)
    for _ in range(3000):
        state.update(machine)
        if machine.transitions:
            break
    assert machine.transitions == ["menu"]


def test_startup_is_skipped_by_a_keypress():
    machine = FakeMachine()
    state = StartupState()
    state.enter(machine)
    press(0)
    state.update(machine)
    assert machine.transitions == ["menu"]


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


def test_the_menu_marks_the_selection_with_a_cursor(setup_menu=None):
    """A filled highlight bar is a 128x10 block switching on every scroll step.

    That is the largest change this panel can make and the noisiest thing the
    display can do to the audio, so the selection is marked with a character.
    """
    import utils

    utils._menu_screen = None
    items = [{"pretty": "alpha"}, {"pretty": "beta"}, {"pretty": "gamma"}]
    utils.show_menu(items, 2, 0)
    lines = [utils._menu_screen.line(i) for i in range(3)]
    assert lines[1].startswith(">"), lines
    assert lines[0].startswith(" ") and lines[2].startswith(" "), lines


def test_the_menu_reuses_one_screen_across_calls():
    """Rebuilding the scene graph each call would resend the whole display."""
    import utils

    utils._menu_screen = None
    items = [{"pretty": "alpha"}, {"pretty": "beta"}]
    utils.show_menu(items, 1, 0)
    first = utils._menu_screen
    utils.show_menu(items, 2, 0)
    assert utils._menu_screen is first


def test_the_menu_blanks_lines_past_the_end_of_a_short_list():
    import utils

    utils._menu_screen = None
    utils.show_menu([{"pretty": "only"}], 1, 0)
    assert utils._menu_screen.line(1) == " "
    assert utils._menu_screen.line(2) == " "


# --- getting the display back ---------------------------------------------
#
# Every state points the display at a group of its own, so the menu does not
# keep it just because it had it once. A menu that updates labels while the
# display is showing somebody else's group looks frozen: scrolling and backing
# out both appear to do nothing, because the drawing is real but invisible.


def test_entering_the_menu_puts_the_menu_on_the_display():
    import utils

    menu = MenuState()
    menu.enter(FakeMachine())
    assert setup.display.shown is utils._menu_screen.group


def test_the_menu_takes_the_display_back_from_another_state():
    """Returning from the sampler, or any state that shows its own group."""
    import displayio

    import utils

    machine = FakeMachine()
    menu = MenuState()
    menu.enter(machine)  # the menu owns the display

    somebody_else = displayio.Group()
    setup.display.show(somebody_else)  # another state takes it
    assert setup.display.shown is somebody_else

    menu.enter(machine)  # and we come back
    assert setup.display.shown is utils._menu_screen.group


def test_scrolling_does_not_reattach_the_display():
    """Reattaching is a full-screen redraw, the loudest thing on this panel.

    It belongs on state entry, which happens once, not on the scroll path,
    which happens for every detent.
    """
    machine = FakeMachine()
    menu = MenuState()
    menu.enter(machine)
    attached = setup.display.shown

    calls = []
    real = setup.display.show
    setup.display.show = lambda group: (calls.append(group), real(group))[1]
    try:
        for _ in range(10):
            show_menu(menu.menu_items, menu.highlight, menu.shift)
    finally:
        setup.display.show = real
    assert calls == [], "show_menu reattached the display"
    assert setup.display.shown is attached


def test_the_menu_still_redraws_late_in_the_tick_period():
    """ticks_ms counts from an unspecified point and wraps at 2**29.

    A zero sentinel for "not drawn yet" is not neutral: once the counter is
    more than half a period past it, the wrap-safe difference goes negative
    and the redraw is never due again, so scrolling stops updating the
    screen for as long as that state is on.
    """
    import utils

    ticks = circuitpython_stubs.ticks
    before = ticks.value
    try:
        ticks.value = (1 << 28) + 5000  # past half the tick period
        machine = FakeMachine()
        menu = MenuState()
        menu.enter(machine)
        utils._menu_screen.set_line(0, "stale")

        setup.select_enc.position = 1
        for _ in range(500):
            menu.update(machine)

        assert utils._menu_screen.line(0) != "stale", "menu never redrew"
    finally:
        ticks.value = before


# --- every screen draws through screen.py --------------------------------
#
# The point of the shared screen is that no state hand-rolls its own drawing.
# A state that shows text without taking the display, or takes it without
# drawing, looks frozen - which is exactly how the Flashy menu broke when
# show_menu stopped drawing on the caller's behalf.


@pytest.mark.parametrize(
    "state_class", [MenuState, FlashyState, MIDIState, HIDState, StartupState]
)
def test_entering_a_state_puts_its_text_on_the_display(state_class):
    state = state_class()
    state.enter(FakeMachine())
    shared = screen_module.shared(setup.display)
    assert setup.display.shown is shared.group, (
        "%s did not take the display" % state_class.__name__
    )


@pytest.mark.parametrize("state_class", [MenuState, FlashyState, MIDIState, HIDState])
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
    assert "watchdog" in source
    assert "feed()" in source, "an armed watchdog that is never fed just resets"


def test_the_watchdog_is_fed_inside_the_loop():
    """Feeding outside the loop arms a reset that always fires."""
    source = open(os.path.join(PRODUCTION_DIR, "main.py")).read()
    loop = source.index("while True:")
    assert source.index("watchdog.feed()", loop) > loop


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
    for state_class in (MenuState, FlashyState, MIDIState, HIDState, StartupState):
        state = state_class()
        state.enter(FakeMachine())
        screens.add(id(setup.display.shown))
    sampler = SamplerState()
    sampler.enter(FakeMachine())
    screens.add(id(setup.display.shown))
    assert len(screens) == 1, "%d different groups were shown" % len(screens)
