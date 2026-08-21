"""Tests for holding the watchdog off during something slow.

The watchdog is armed at two seconds, and the badge deliberately does things
that take longer - importing a screen off the card, listing a directory,
creating one. A reset in the middle of those looks exactly like a crash: the
badge reboots with no traceback, because a reset is not an exception.

The stub watchdog records what was done to it, so the ordering that matters -
off, then the slow thing, then on again, even if the slow thing raised - is
checked here rather than by watching a badge reboot.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
import guard
import microcontroller


@pytest.fixture
def dog():
    watchdog = microcontroller.watchdog
    watchdog.mode = None
    watchdog.timeout = None
    return watchdog


def test_arming_sets_a_timeout_and_a_mode(dog):
    assert guard.arm() is True
    assert dog.timeout == guard.TIMEOUT
    assert dog.mode is guard.RESET


def test_patience_turns_it_off(dog):
    guard.arm()
    guard.patience()
    assert dog.mode is None, "the watchdog was still armed during a slow call"


def test_resume_puts_it_back(dog):
    guard.arm()
    previous = guard.patience()
    guard.resume(previous)
    assert dog.mode is guard.RESET
    assert dog.timeout == guard.TIMEOUT


def test_slowly_runs_the_work_with_it_off(dog):
    guard.arm()
    seen = []
    guard.slowly(lambda: seen.append(dog.mode))
    assert seen == [None], "the slow call ran with the watchdog still armed"
    assert dog.mode is guard.RESET, "it was not put back"


def test_slowly_returns_what_the_work_returned(dog):
    guard.arm()
    assert guard.slowly(lambda: 42) == 42


def test_the_watchdog_comes_back_even_if_the_work_raises(dog):
    """A badge left with no watchdog is worse than one that resets early."""
    guard.arm()
    with pytest.raises(OSError):
        guard.slowly(_explode)
    assert dog.mode is guard.RESET


def test_a_raising_work_still_propagates(dog):
    guard.arm()
    with pytest.raises(OSError):
        guard.slowly(_explode)


def test_nothing_breaks_when_no_watchdog_was_ever_armed(dog):
    """Tests and harnesses call these without setting one up."""
    guard.feed()
    previous = guard.patience()
    guard.resume(previous)
    assert guard.slowly(lambda: "fine") == "fine"


def test_resume_without_a_previous_mode_leaves_it_alone(dog):
    """patience() on an unarmed watchdog returns None; resume must not arm one."""
    guard.resume(None)
    assert dog.mode is None


def _explode():
    raise OSError("the card went away")


def test_the_timeout_cannot_exceed_what_the_chip_allows(dog):
    """Measured on the badge: larger raises "timeout must be <= 8".

    This is why a slow card operation turns the watchdog off rather than
    merely widening it - creating a directory has been measured at eight to
    nine seconds, which is past the largest timeout available.
    """
    with pytest.raises(ValueError):
        guard.arm(timeout=30.0)


# --- the paths that are actually slow -------------------------------------


def _watch(module, seen):
    """Replace guard.slowly so it records the mode *during* the slow call."""
    real = module.guard.slowly

    def watched(work, timeout=guard.TIMEOUT):
        def observed():
            seen.append(module.guard._watchdog().mode)
            return work()

        return real(observed, timeout)

    module.guard.slowly = watched
    return real


def test_building_a_screen_holds_the_watchdog_off(dog):
    """Compiling one off the card measured 250 to 1500 ms against 2 s armed."""
    import statemachine

    guard.arm()
    machine = statemachine.StateMachine()
    seen = []
    real = _watch(statemachine, seen)
    try:
        machine.state_for("sampler")
    finally:
        statemachine.guard.slowly = real
    assert seen == [None], "the import ran with the watchdog armed"
    assert dog.mode is guard.RESET, "the watchdog was not put back"


def test_card_work_in_settings_holds_the_watchdog_off(dog):
    """Creating the songs directory measured eight to nine seconds."""
    import SettingsState

    guard.arm()
    state = SettingsState.SettingsState()
    seen = []
    state._quietly(lambda: seen.append(dog.mode))
    assert seen == [None], "the card was touched with the watchdog still armed"
    assert dog.mode is guard.RESET


def test_warming_the_card_holds_the_watchdog_off(dog):
    """Warming runs inside the main loop, so the watchdog is already armed."""
    import SettingsState

    guard.arm()
    state = SettingsState.SettingsState()
    state._warmed = 0
    modes = []
    real = _watch(SettingsState, modes)
    try:
        state.warm_step()
    finally:
        SettingsState.guard.slowly = real
    assert modes == [None], "a card read ran with the watchdog armed"
    assert dog.mode is guard.RESET, "the watchdog was not put back"
