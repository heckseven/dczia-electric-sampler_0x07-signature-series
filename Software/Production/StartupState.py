import time
import screen
from State import State
from setup import (
    display,
    keys,
    neopixels,
)

# Imported during the banner, in the order they are needed. The sequencer
# is first because it is the largest single cost and everything else is
# quick beside it; SettingsState is last because it imports most of the
# rest, so by the time it is reached there is little left to do.
WARM = (
    "prefs",
    "sequencer",
    "store",
    "songfile",
    "kitfile",
    "engine.menu",
    "engine.editor",
    "engine.naming",
    "engine.settings",
    "SettingsState",
)


class StartupState(State):
    color = (0, 0, 0)
    timer = 0
    stage = 0
    warmed = 0

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
        # One slow thing per pass, alongside the animation rather than
        # instead of it. The work is the same either way, and a badge whose
        # lights keep moving does not look like one that has hung.
        self._warm_step(machine)
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
                self._warm(machine)
                machine.go_to_state("sampler")
        # Skip the banner if a key is pressed.
        key_event = keys.events.get()
        if key_event and key_event.pressed:
            self._warm(machine)
            machine.go_to_state("sampler")

    def _warm(self, machine):
        """Finish warming. Whatever is left runs before the sampler does."""
        while self._warm_step(machine):
            pass

    def _warm_step(self, machine):
        """Do one slow thing. Called once per pass, so the banner keeps moving.

        Every other screen is imported the first time it is asked for, which
        keeps the memory it costs off a badge that is only playing a pattern.
        The settings tree cannot afford that: measured on the badge, its
        modules take 1.3 seconds to compile off the card, and the audio
        buffer holds 32 milliseconds. Opening it mid-pattern would be forty
        buffers of silence.

        It is also the one screen every player reaches - there is no other
        way to a song, a sample or a tool - so the memory is going to be
        spent in any case. This only moves the cost to the one moment in the
        badge's life when a pause costs nothing.

        One per pass rather than all at once because the whole list is about
        twelve seconds, most of it the sequencer allocating the audio path
        and reading the default kit off the card. Done in a single call the
        banner freezes for all of it and the badge looks hung; spread across
        the passes the animation is making anyway, it is invisible.
        """
        if self.warmed < len(WARM):
            name = WARM[self.warmed]
            self.warmed += 1
            try:
                __import__(name)
            except (ImportError, MemoryError):
                # Warming is an optimisation. A module that will not import
                # here will fail the same way when it is really needed, and
                # with a screen up to say so rather than in the middle of the
                # banner.
                pass
            return True
        # Then the card. Listing a directory over SPI measured 500 to 1000 ms
        # on this hardware and the sample list is read by all eight track
        # rows, so none of it can be afforded once a pattern is playing.
        try:
            return machine.state_for("settings").warm_step()
        except (ImportError, OSError, MemoryError, KeyError):
            # ImportError for the same reason as above: state_for imports the
            # screen, and a module that will not compile must fail where
            # there is something on screen to say so, not in the middle of
            # the banner.
            return False
