"""The animation screen: lights that move with the beat.

Pick one with the encoder, watch it, press anything to go back. What is
different from the version this replaces is where the motion comes from. The
old one drove adafruit_led_animation at a fixed number of seconds a step, so
a chase matched a 120 BPM pattern only by coincidence and any other tempo not
at all. These are functions of the sequencer's tick, so they lock to the
tempo - and, because engine/clock.py latches to an external clock and
flywheels through gaps in it, to a drum machine on the sync input as well.

The animations themselves are pure logic in engine/animation.py, where what
the strip shows on a given sixteenth is a test rather than something judged by
waving a badge about. This is the part that needs hardware: reading the knob,
choosing a tick, and pushing bytes at the strip.

Two costs are managed here. Pushing ten pixels is not free and neither is
building the colours, so frames are paced rather than drawn every pass of a
loop that turns over thousands of times a second; and a frame identical to the
one before it is not pushed at all, which is most of them at a slow tempo.
"""

from supervisor import ticks_ms

import screen as screen_module
from engine import animation
from engine.clock import ticks_diff
from engine.menu import Item, Menu
from sequencer import engine as sequencer
from setup import display, keys, neopixels, select_enc
from State import State

# Smallest gap between frames. The animations step on the sixteenth, which at
# 120 BPM is 125 ms and at the 300 BPM ceiling is 50 ms, so 25 ms is finer
# than the fastest thing they do while leaving the loop to the audio. Measured
# cost of a ten pixel show() is well under a millisecond, but it is a bit-
# banged write with interrupts off, and doing it thousands of times a second
# for no visible gain is heat and jitter rather than animation.
MIN_FRAME_MS = 25

# How bright, before an animation dims anything further. Full, because the
# strip is already scaled: setup.py builds it at brightness=0.1, so this is
# a second stage on top of that one and anything less here would make the
# animations dimmer than the sampler's own pads.
BRIGHTNESS = 1.0

MENU_ROWS = 2
WIDTH = 21


class FlashyState(State):
    """Choosing and watching an animation."""

    @property
    def name(self):
        return "flashy"

    def __init__(self):
        self.menu = Menu(
            Item("Flashy", children=[Item(label) for label in animation.NAMES]),
            rows=MENU_ROWS,
        )
        self._screen = screen_module.shared(display)
        self._animation = animation.by_name(animation.NAMES[0])
        self._last_select = 0
        self._last_frame = None
        self._last_colors = None
        self._stale = True

    # --- the state machine ------------------------------------------------

    def enter(self, machine):
        # The press that arrived here is still in the queue; its release would
        # otherwise be read as the keypress that leaves again.
        keys.events.clear()
        self._last_select = select_enc.position
        self._last_frame = None
        self._last_colors = None
        self._stale = True
        self._screen.attach()
        self._screen.set_lines(self._lines())
        self._screen.flush_all()
        State.enter(self, machine)

    def exit(self, machine):
        neopixels.fill(animation.OFF)
        neopixels.show()
        State.exit(self, machine)

    def update(self, machine):
        position = select_enc.position
        if position != self._last_select:
            self.menu.move(position - self._last_select)
            self._last_select = position
            self._animation = animation.by_name(self.menu.selected.label)
            self._stale = True

        # Any key leaves, which is what the screen has always done. Read
        # before the frame so leaving is never a frame late.
        event = keys.events.get()
        if event and event.pressed:
            machine.go_to_state("settings")
            return

        if self._stale:
            self._screen.set_lines(self._lines())
            self._stale = False
        self._screen.flush()
        self._draw()

    # --- the lights -------------------------------------------------------

    def _tick(self):
        """The beat to draw, running whether or not the transport is.

        A panel that goes dead the moment the pattern stops looks broken, so
        a stopped clock falls back to a tick derived from elapsed time at the
        same tempo. Starting the transport therefore does not visibly jump.
        """
        clock = sequencer.clock
        if clock.running:
            return clock.tick
        return animation.free_running_tick(ticks_ms(), clock.bpm)

    def _draw(self):
        now = ticks_ms()
        if self._last_frame is not None:
            if ticks_diff(now, self._last_frame) < MIN_FRAME_MS:
                return
        self._last_frame = now
        colors = self._animation(self._tick(), BRIGHTNESS)
        # At a slow tempo most frames are the same as the one before, and a
        # show() that changes nothing is still a write with interrupts off.
        if colors == self._last_colors:
            return
        self._last_colors = colors
        for index in range(animation.PIXEL_COUNT):
            neopixels[index] = colors[index]
        neopixels.show()

    # --- the screen -------------------------------------------------------

    def _lines(self):
        position, total = self.menu.position
        heading = "Flashy"
        if total > MENU_ROWS:
            count = " %d/%d" % (position, total)
            heading = "%-*s%s" % (WIDTH - len(count), heading, count)
        return [heading] + self.menu.rendered(WIDTH)[:MENU_ROWS]
