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

import microcontroller
from watchdog import WatchDogMode

from sequencer import engine
from statemachine import StateMachine

# Generous on purpose: the loop turns over thousands of times a second and the
# longest thing it does deliberately, drawing a line of text, is measured in
# tens of milliseconds. The badge has hung with the CPU inside a peripheral
# driver where a KeyboardInterrupt cannot reach it, and the reset button is
# not reachable when the badge is mounted, so restarting itself is the only
# recovery the player has.
WATCHDOG_TIMEOUT = 2.0

machine = StateMachine()
machine.go_to_state("startup")

# Armed after the kit has loaded: reading the card is legitimately slower than
# anything the loop does later.
watchdog = microcontroller.watchdog
watchdog.timeout = WATCHDOG_TIMEOUT
watchdog.mode = WatchDogMode.RESET

while True:
    watchdog.feed()
    # The beat runs regardless of what is on screen.
    engine.tick()
    machine.update()
