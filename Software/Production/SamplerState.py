"""The sampler's user interface.

This state renders and handles input. It owns no audio and no clock: the
engine is a singleton ticked from the main loop, so the beat carries on
whether or not this state is the one on screen. Leaving here for the menu
stops nothing.

Redrawing is throttled and change-driven. A full 128x32 frame costs about
13 ms over I2C even at 400 kHz, and the main loop runs every 250 us or so;
redrawing on every pass would spend almost all of the badge's time pushing
identical pixels and would drag the sequencer with it.
"""

import screen as screen_module
from engine import view
from engine.controls import (
    ARM_RECORD,
    BACK,
    CLEAR_TRACK,
    ERASE,
    MUTE,
    PAD,
    PAD_RELEASE,
    PAGE,
    SELECT_TRACK,
    TOGGLE_MODE,
    TOGGLE_TRANSPORT,
    Controls,
)
from engine.song import MAX_VELOCITY, MIN_VELOCITY
from engine.transport import SEQ
from sequencer import engine as sequencer
from setup import display, keys, neopixels, select_enc, volume_enc  # noqa: F401
from State import State
from utils import neoindex

# How long a struck pad stays lit, in main-loop passes. Long enough to see,
# short enough not to smear at speed.
FLASH_PASSES = 40

# How often the display is even considered for redrawing, in main-loop passes.
#
# Sending a frame is expensive and, more importantly, noisy: measured at about
# 32 ms of I2C traffic, and that traffic audibly pops the amplifier through the
# shared supply. Confirmed on the badge - identical work with the bus silent
# produces no pops, and a slower bus produces more of them, because each frame
# occupies it for longer.
#
# Reusing Label objects does not help: measured, mutating a label's text costs
# 31 ms against 33 ms for rebuilding the group, because displayio resends
# essentially the whole frame either way. A refresh with nothing changed costs
# 122 us. So the only lever is how often the content changes at all.
REDRAW_EVERY = 400


class SamplerState(State):
    @property
    def name(self):
        return "sampler"

    def __init__(self):
        self.controls = Controls()
        self._flash = {}
        # Pads held in SEQ whose velocity has been edited during the hold.
        # Editing suppresses the toggle on release, so adjusting a step's level
        # does not also switch the step off underneath you.
        self._edited = set()
        self._last_select = 0
        self._last_volume = 0
        self._passes = 0
        self._shown = None
        self._last_pixels = None
        # One label per line. A single label spanning the screen would make
        # every change a full-screen resend, which is audible; see screen.py.
        self._screen = screen_module.TextScreen(lines=3)
        self._attached = False
        State.__init__(self)

    # --- lifecycle --------------------------------------------------------

    def enter(self, machine):
        keys.events.clear()
        self.controls = Controls(mode=sequencer.mode)
        self._last_select = select_enc.position
        self._last_volume = volume_enc.position
        self._shown = None
        self._last_pixels = None
        self._edited = set()
        self._screen.attach(display)
        self._attached = True
        self._render(force=True)
        State.enter(self, machine)

    def exit(self, machine):
        # The beat keeps playing; only the display and pads stop being ours.
        self._attached = False
        State.exit(self, machine)

    # --- input ------------------------------------------------------------

    def update(self, machine):
        self._passes += 1
        leaving = self._handle_keys(machine)
        if leaving:
            return
        self._handle_encoders()
        self._expire_flashes()
        self._render()

    def _handle_keys(self, machine):
        event = keys.events.get()
        while event:
            for action, value in self.controls.handle(event.key_number, event.pressed):
                if self._act(action, value, machine):
                    return True
            event = keys.events.get()
        return False

    def _act(self, action, value, machine):
        """Returns True if the state is being left."""
        if action == PAD:
            if self.controls.mode == SEQ:
                # Nothing happens yet: the press might be the start of a
                # velocity edit. Deciding on release is what lets one gesture
                # mean both "toggle this step" and "adjust this step".
                self._edited.discard(value)
            else:
                sequencer.pad_hit(value)
                self._flash[value] = FLASH_PASSES
        elif action == PAD_RELEASE:
            if self.controls.mode == SEQ and value not in self._edited:
                self._toggle_step(value)
            self._edited.discard(value)
        elif action == SELECT_TRACK:
            sequencer.select_track(value)
        elif action == PAGE:
            sequencer.set_page(value)
        elif action == ERASE:
            sequencer.erase(value)
        elif action == TOGGLE_MODE:
            self.controls.set_mode(sequencer.toggle_mode())
        elif action == TOGGLE_TRANSPORT:
            sequencer.toggle_play()
        elif action == ARM_RECORD:
            sequencer.toggle_record()
        elif action == MUTE:
            sequencer.song.toggle_mute(sequencer.selected_track)
        elif action == CLEAR_TRACK:
            sequencer.song.clear_track(sequencer.selected_track)
        elif action == BACK:
            machine.go_to_state("menu")
            return True
        self._shown = None  # any action may have changed what is displayed
        return False

    def _toggle_step(self, slot):
        step = sequencer.page * 8 + slot
        if step >= sequencer.song.length:
            return
        track = sequencer.selected_track
        turned_on = sequencer.song.toggle_step(track, step)
        if turned_on:
            # Audition the change, so editing is audible without playing.
            sequencer.trigger(track, sequencer.song.velocity(track, step))

    # --- encoders ---------------------------------------------------------

    def _handle_encoders(self):
        position = select_enc.position
        if position != self._last_select:
            self._select_turned(position - self._last_select)
            self._last_select = position
            self._shown = None

        position = volume_enc.position
        if position != self._last_volume:
            self._volume_turned(position - self._last_volume)
            self._last_volume = position
            self._shown = None

    def _select_turned(self, delta):
        target = self.controls.select_turn_target()
        song = sequencer.song
        if target == "length":
            song.set_length(song.length + delta)
            sequencer.set_page(min(sequencer.page, song.page_count - 1))
        elif target == "step_velocity":
            self._nudge_velocity(delta)
        else:
            sequencer.set_bpm(sequencer.clock.bpm + delta)

    def _volume_turned(self, delta):
        target = self.controls.volume_turn_target()
        song = sequencer.song
        if target == "division":
            song.set_division(song.division + delta)
        elif target == "quantize":
            sequencer.nudge_strength(1 if delta > 0 else -1)
        # Master volume is not implemented yet; the knob is otherwise inert.

    def _nudge_velocity(self, delta):
        """Holding a pad and turning Select edits that step's level."""
        song = sequencer.song
        track = sequencer.selected_track
        for slot in self.controls.held_pads:
            step = sequencer.page * 8 + slot
            if step >= song.length or not song.is_on(track, step):
                continue
            level = song.velocity(track, step) + delta * 4
            song.set_velocity(track, step, max(MIN_VELOCITY, min(MAX_VELOCITY, level)))
            # This hold is an edit, so releasing must not toggle the step off.
            self._edited.add(slot)

    # --- rendering --------------------------------------------------------

    def _expire_flashes(self):
        if not self._flash:
            return
        for pad in list(self._flash):
            self._flash[pad] -= 1
            if self._flash[pad] <= 0:
                del self._flash[pad]

    def _render(self, force=False):
        playhead = sequencer.current_step if sequencer.transport.playing else None
        self._render_pixels(playhead)
        if force or self._passes % REDRAW_EVERY == 0:
            self._render_display()

    def _render_pixels(self, playhead):
        song = sequencer.song
        loaded = [sequencer.has_sample(t) for t in range(8)]
        blink = (self._passes // 200) % 2 == 0
        colors = view.pads(
            song,
            self.controls.mode,
            loaded,
            track=sequencer.selected_track,
            page=sequencer.page,
            playhead=playhead,
            flashing=set(self._flash),
        )
        colors = list(colors)
        colors.append(view.play_indicator(sequencer.transport, blink))
        colors.append(
            view.function_indicator(self.controls.mode, sequencer.clock, blink)
        )
        if colors == self._last_pixels:
            return
        self._last_pixels = colors
        for key_number in range(10):
            neopixels[neoindex(key_number)] = colors[key_number]
        neopixels.show()

    def _render_display(self):
        song = sequencer.song
        top = view.status_line(
            song,
            self.controls.mode,
            sequencer.selected_track,
            sequencer.page,
            sequencer.transport,
            sequencer.clock,
        )
        middle = view.detail_line(song, sequencer.clock)
        # The playhead is deliberately absent. Including it would change the
        # text on every step, so a frame would be sent on every step, and
        # frames pop the amplifier. The playhead lives on the pad LEDs
        # instead, which cost about 2 ms and are electrically quiet.
        bottom = view.step_row(song, sequencer.selected_track, sequencer.page)
        # Set each line separately: only lines that differ are resent.
        self._screen.set_lines((top, middle, bottom))
        self._shown = "%s\n%s\n%s" % (top, middle, bottom)
