"""The watchdog, and how to do something slow without it firing.

The badge has hung with the CPU inside a peripheral driver, where a
KeyboardInterrupt cannot reach it and the reset button is not reachable
either once the badge is mounted in a rack. The watchdog is the only recovery
the player has, so it is armed tightly - two seconds, against a main loop
that turns over thousands of times a second.

That tightness is the problem this module exists to solve. Some things the
badge does deliberately take longer than two seconds, and every one of them
is on the SD card:

    importing a screen off the card        250-1500 ms
    listing a directory                    500-1000 ms
    creating a directory                  8000-9500 ms

A watchdog reset in the middle of one of those looks exactly like a crash -
the badge reboots, with no error screen and no traceback, because a reset is
not an exception. It is also the wrong answer: the badge is not hung, it is
waiting for a slow card, and rebooting will simply make it wait again.

Raising the timeout is not enough by itself. The RP2040 caps it at eight
seconds - anything larger raises "timeout must be <= 8", measured - and
creating a directory has been seen to take longer than that. So a slow
operation turns the watchdog off and turns it back on afterwards. That is
safe precisely because these are the operations the badge *knows* it is in:
the risk the watchdog covers is an unexpected hang, not a call the firmware
deliberately made and is waiting on.

Everything here tolerates a watchdog that was never armed, so the tests and
any harness can call it without setting one up.
"""

import microcontroller

try:
    from watchdog import WatchDogMode

    RESET = WatchDogMode.RESET
except ImportError:  # pragma: no cover - present on every board this runs on
    RESET = None

# Generous for a loop whose longest deliberate act, drawing a line of text,
# is measured in tens of milliseconds.
TIMEOUT = 2.0


def _watchdog():
    try:
        return microcontroller.watchdog
    except AttributeError:
        return None


def arm(timeout=TIMEOUT):
    """Start the watchdog. Returns whether there is one to start."""
    dog = _watchdog()
    if dog is None or RESET is None:
        return False
    dog.timeout = timeout
    dog.mode = RESET
    return True


def feed():
    """Tell the watchdog the loop is still turning."""
    dog = _watchdog()
    if dog is None:
        return
    try:
        dog.feed()
    except (ValueError, AttributeError):
        # Feeding one that was never armed. Not an error worth raising from
        # the middle of a main loop.
        pass


def patience():
    """Turn the watchdog off for something slow the badge meant to do.

    Returns what to hand back to `resume`. Always pair the two, and put the
    `resume` in a finally: a slow operation that raises would otherwise leave
    the badge with no watchdog at all, which is the one state worse than a
    watchdog that fires too eagerly.
    """
    dog = _watchdog()
    if dog is None:
        return None
    try:
        previous = dog.mode
        dog.mode = None
        return previous
    except (ValueError, AttributeError):
        return None


def resume(previous, timeout=TIMEOUT):
    """Put the watchdog back the way `patience` found it."""
    if previous is None:
        return
    dog = _watchdog()
    if dog is None:
        return
    try:
        dog.timeout = timeout
        dog.mode = previous
        dog.feed()
    except (ValueError, AttributeError):
        pass


def slowly(work, timeout=TIMEOUT):
    """Run something slow with the watchdog held off, and always restore it."""
    previous = patience()
    try:
        return work()
    finally:
        resume(previous, timeout)
