import time
import screen
from State import State
from setup import (
    display,
    keys,
    neopixels,
)


class StartupState(State):
    color = (0, 0, 0)
    timer = 0
    stage = 0

    @property
    def name(self):
        return "startup"

    def enter(self, machine):
        neopixels.fill((0, 0, 0))
        neopixels.show()
        self._screen = screen.shared(display)
        self._screen.attach()
        self._screen.clear()
        self._screen.flush_all()
        State.enter(self, machine)

    def exit(self, machine):
        neopixels.fill((255, 0, 0))
        neopixels.show()
        self.color = (0, 0, 0)
        self.timer = 0
        self.stage = 0
        State.exit(self, machine)

    def _reveal(self, text):
        """Type the banner out a character at a time.

        The old version built a whole new Label on every pass and pushed a
        full frame with it. Setting lines on the shared screen instead means
        the one new character is the only cell that gets written.
        """
        shown = text[: self.timer] if len(text) > self.timer else text
        lines = shown.split("\n")
        for index in range(len(self._screen)):
            self._screen.set_line(index, lines[index] if index < len(lines) else "")
        self._screen.flush(budget=len(self._screen))

    def update(self, machine):
        self.timer = self.timer + 1
        if self.stage == 0:
            text = "       DCZia\n  Electric Sampler"
            self._reveal(text)
            self.color = (self.timer, self.timer, 0)
            if self.timer > (len(text) * 1.5):
                self.timer = 0
                self.stage = 1
        elif self.stage == 1:
            text = "Fueled by Green Chile\n     and Solder"
            self._reveal(text)
            if self.timer > (len(text) * 1.5):
                self.timer = 0
                self.stage = 2
        else:
            if self.timer < (255 * 8):
                color = (0, self.timer % 255, 0)
                neopixels[self.timer // 255] = color
                neopixels.show()
                self.timer = self.timer + 1  # make it faster
            else:
                time.sleep(0.1)
                machine.go_to_state("menu")
        # Skip to menu if encoder is pressed
        key_event = keys.events.get()
        if key_event and key_event.pressed:
            machine.go_to_state("menu")
