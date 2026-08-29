import gc
import time

import guard
import screen
from State import State
from setup import (
    display,
    keys,
    neopixels,
)

# Imported during the banner, one per pass, in the order they are needed: the
# audio engine and the settings tree first, then the four modules nothing
# earlier in the list pulls in, then the sampler screen itself.
#
# "sequencer" is a no-op here and kept on purpose. main imports it at module
# scope, before the banner exists, so warming finds it already loaded -
# measured, free memory moved 32 bytes across that pass where a 48 KB module
# would have shown. It stays listed so this remains a complete statement of
# what has to be up before the sampler runs, rather than one that quietly
# depends on an import in another file.
#
# SamplerState is in this list because `state_for` compiles a screen the first
# time it is asked for, and the sampler used to be asked for at the very end of
# the banner - after every other module and the whole settings tree had been
# built, which is the most fragmented the heap ever gets. Compiling 24 KB of
# source needs a lot of contiguous memory and there was none: measured on the
# badge, 22 KB free at that point, and a gc.collect() lifting it to 42 KB still
# could not satisfy a 195-byte allocation. The badge rebooted in a loop.
#
# Deferring it bought nothing anyway. Every other screen is deferred so that a
# badge only playing a pattern does not pay for them; the sampler IS that
# badge, reached on every boot without exception. The memory is spent either
# way - the only choice is whether it is spent on a clean heap or a ruined one.
#
# engine.animation, engine.view, engine.controls and utils are named here for
# the same reason the list exists at all: nothing *earlier in this list* pulls
# them in. The sequencer covers engine.clock, song, transport, util, quantize
# and wav, and the settings modules cover the rest, but those four are reached
# first by SamplerState - so without them its pass compiles 62 KB rather than
# 24 KB, in one pass, which is the banner freeze this whole mechanism exists to
# avoid. (They are not the sampler's alone: FlashyState imports animation, and
# HIDState and MIDIState import utils. Being unwarmed until now is what they
# have in common, not being exclusive to the sampler.) One per pass only works if every entry
# really is one module's worth of work, and
# test_the_sampler_pass_imports_only_the_sampler is what keeps it that way.
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
    "engine.animation",
    "engine.view",
    "engine.controls",
    "utils",
    "SamplerState",
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
        # The banner allocates on every one of its ~1100 passes and nothing
        # collects while it runs: main's loop only collects below GC_FLOOR,
        # and the heap sits above it the whole time. Measured, that leaves
        # about 20 KB of garbage standing at exactly the moment the sampler
        # is built. Collecting is 28-48 ms, which is free here and nowhere
        # else - the badge is already showing a banner and no audio is
        # playing yet.
        gc.collect()
        # Said once, over USB serial, because it is the number every memory
        # decision on this badge is made against and there is no other way to
        # see it. What is left here has to cover the settings tree building a
        # sample list, which is one Item per row - so a badge whose sample
        # browser has started coming up empty says why here first.
        # Imported here rather than at the top: main.py has already loaded it
        # by the time this runs, so it costs nothing, and StartupState stays
        # importable without pulling the audio path in behind it.
        from sequencer import engine

        # The sample count used to be on this line. Measuring it meant asking
        # the catalog for the listing, which read the card and then held the
        # 12 KB it came back with for the whole session - exactly what
        # SettingsState stopped doing, and the reason the sampler had no room
        # left to play in. A diagnostic that costs more than what it reports
        # is not worth having. When a listing fails now the menu says "Out of
        # memory" and list_samples raises rather than answering "none".
        print("free after warm: %d, kit %d" % (gc.mem_free(), engine.ram_used))

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
                # Held off for the same reason statemachine.state_for holds it
                # off: compiling a module off the card is slower than the
                # watchdog, and _warm runs the whole remaining list inside a
                # single pass when the banner is cut short by a key press -
                # with nothing feeding the dog in between. A reset there looks
                # exactly like a crash, because a reset is not an exception.
                guard.slowly(lambda: __import__(name))
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
