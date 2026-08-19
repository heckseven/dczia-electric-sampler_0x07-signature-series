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

import gc

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

# Collect before the heap is empty rather than when it is.
#
# A loop pass allocates about two bytes, which sounds like nothing until it
# is multiplied by a few thousand passes a second. Left alone, MicroPython
# runs the heap down to nothing and collects only when an allocation fails -
# measured on the badge, free memory reaching 112 bytes between collections
# while a pattern played. The audio path allocates from the same heap, from
# a background task, at a moment nothing here controls, and an allocation
# that fails there is not an exception that can be caught: it is a hard
# fault, which is how this badge has been dying.
#
# Checking is free - gc.mem_free() allocates nothing and is fast - so the
# floor is checked every pass and a collection happens while there is still
# room, at a point in the loop where nothing is mid-flight.
GC_FLOOR = 16 * 1024

machine = StateMachine()
machine.go_to_state("startup")

# Armed after the kit has loaded: reading the card is legitimately slower than
# anything the loop does later.
watchdog = microcontroller.watchdog
watchdog.timeout = WATCHDOG_TIMEOUT
watchdog.mode = WatchDogMode.RESET

while True:
    watchdog.feed()
    if gc.mem_free() < GC_FLOOR:
        gc.collect()
    # The beat runs regardless of what is on screen.
    engine.tick()
    machine.update()
