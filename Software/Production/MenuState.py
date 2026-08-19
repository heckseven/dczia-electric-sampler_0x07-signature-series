from adafruit_led_animation.animation.rainbow import Rainbow
from supervisor import ticks_ms

from engine.clock import ticks_diff
from State import State
from utils import show_menu
from setup import (
    neopixels,
    select_enc,
    keys,
)

# Sending a frame costs about 32 ms of I2C traffic and audibly pops the
# amplifier through the shared supply. Scrolling quickly would otherwise queue
# a frame per detent, so redraws are coalesced: the position is always current,
# but the screen is only pushed this often.
REDRAW_INTERVAL_MS = 120


class MenuState(State):
    menu_items = [
        {
            "name": "flashy",
            "pretty": "Flashy",
        },
        {
            "name": "sampler",
            "pretty": "Sampler",
        },
        {
            "name": "midi_controller",
            "pretty": "MIDI Controller",
        },
        {
            "name": "hid",
            "pretty": "USB HID Mode",
        },
    ]

    @property
    def name(self):
        return "menu"

    def __init__(self):
        self.total_lines = 3
        self.list_length = len(self.menu_items)
        self.highlight = 1
        self.shift = 0

    def enter(self, machine):
        self.last_position = 0
        self._last_draw = 0
        self._dirty = False
        if machine.animation is None:
            machine.animation = Rainbow(neopixels, speed=0.1)
        show_menu(self.menu_items, self.highlight, self.shift)
        State.enter(self, machine)

    def exit(self, machine):
        select_enc.position = 0
        State.exit(self, machine)

    def update(self, machine):
        # Code for moving through menu and selecting mode
        if machine.animation:
            machine.animation.animate()
        # Some code here to use an encoder to scroll through menu options, press to select one
        position = select_enc.position

        if self.last_position != position:
            if position < self.last_position:
                if self.highlight > 1:
                    self.highlight -= 1
                else:
                    if self.shift > 0:
                        self.shift -= 1
            else:
                if self.highlight < self.total_lines:
                    self.highlight += 1
                else:
                    if self.shift + self.total_lines < self.list_length:
                        self.shift += 1
            self._dirty = True
        self.last_position = position

        # Coalesce: scrolling fast must not queue a frame per detent.
        if self._dirty:
            now = ticks_ms()
            # ticks_ms wraps at 2**29; a plain subtraction reads as hugely
            # overdue at the wrap and forces a needless frame, which pops.
            if ticks_diff(now, self._last_draw) >= REDRAW_INTERVAL_MS:
                show_menu(self.menu_items, self.highlight, self.shift)
                self._last_draw = now
                self._dirty = False

        key = keys.events.get()
        if key and key.pressed:
            machine.go_to_state(
                self.menu_items[self.highlight - 1 + self.shift]["name"]
            )
