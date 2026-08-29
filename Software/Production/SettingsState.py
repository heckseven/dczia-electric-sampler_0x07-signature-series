"""The settings screen: a tree, a knob, and two buttons.

Opened with a click of the Select encoder from the sampler. While it is up,
Play and Function stop being transport controls and become yes and no - the
badge has no other buttons to spare, and a menu needs both. Play and the
encoder click both go in, Function comes back out, and Function at the top
closes the screen and returns to the sampler.

The beat carries on throughout. The engine is ticked by the main loop rather
than by whatever is on screen, so saving a song or picking a sample happens
over a pattern that is still playing.

What the tree contains lives in engine/settings.py, how it is walked in
engine/menu.py, and the two ways of entering a value in engine/editor.py and
engine/naming.py. All of those are pure logic and tested without a display.
This is the part that needs hardware: reading keys, pushing text at the
screen, and doing what a command means.

Everything here obeys the same budget as the sampler. Drawing is what
competes with the audio - one line costs about 9 ms against a 32 ms buffer -
so the rows are rebuilt only when something moves, and pushed through the
shared screen's paced flush. Spinning the knob cannot outrun the display
into tearing the sound; the screen simply converges a pass or two later.
"""

from supervisor import ticks_ms

import guard
import kitfile
import prefs
import screen as screen_module
import sequencer as sequencer_module
import songfile
from engine import settings, view
from engine.clock import ticks_diff
from engine.editor import Editor
from engine.menu import Menu
from engine.naming import NameEntry
from engine.song import DIVISIONS, MAX_STEPS, MIN_LENGTH, TRACK_COUNT
from sequencer import engine as sequencer
from setup import display, keys, neopixels, select_enc, volume_enc
from State import State
from utils import neoindex
from store import StoreError

# Key numbers, named here rather than imported from the sampler's controls:
# while this screen is up they mean something else entirely, and borrowing
# the sampler's names for them would suggest otherwise.
PLAY_KEY = 8
FUNCTION_KEY = 9
SELECT_KEY = 10
VOLUME_KEY = 11

# How long a confirmation stays on screen. Long enough to read a few words,
# short enough that the badge does not seem to have stopped responding.
MESSAGE_MS = 1200

# Key events handled per pass, for the same reason the sampler bounds its
# own: unbounded draining lets a backlog do all of its work in one pass, and
# a pass that long is an audible gap.
MAX_EVENTS_PER_PASS = 2

# What the question at the top of a confirmation screen says. Read from the
# command rather than written once, so a third thing that needs confirming
# cannot silently inherit the word "Delete".
CONFIRM_VERBS = {
    settings.SONG_DELETE: "Delete song?",
    settings.KIT_DELETE: "Delete kit?",
}

# Two rows of the tree, because the top row is the breadcrumb. Nesting is
# unbounded and the labels repeat - Song has Save, and so does Kit - so
# without a line saying where you are, half the leaves are ambiguous.
MENU_ROWS = 2

WIDTH = 21


class Catalog:
    """What settings.build offers from the card and from flash.

    Everything is remembered once read. Measured on the badge, a directory
    listing over SPI is 500 to 1000 ms - thirty audio buffers - and the
    sample list is read by all eight track rows, so without this, walking
    from Track 1 to Track 2 would cost another second of torn sound.

    What is remembered is dropped by `forget`, which is called after this
    badge writes to the card. A card swapped behind its back is not noticed;
    there is no cheap way to ask, and the answer costs a second.
    """

    def __init__(self):
        self._songs = None
        self._kits = None
        self._samples = None

    def songs(self):
        if self._songs is None:
            self._songs = [(name, name) for name in songfile.songs()]
        return self._songs

    def kits(self):
        if self._kits is None:
            self._kits = [(name, name) for name in kitfile.kits()]
        return self._kits

    def samples(self):
        """[(label, path)] - remembered only once it is a real answer.

        An empty list is not cached. Nothing on the badge writes a .wav, so
        this listing is never dropped once it is kept - which means a listing
        that came back empty because the heap was tight at that moment would
        have stayed empty for the rest of the session, and the browser would
        show nothing but "(none)" with no way to ask again.

        Flash always holds the shipped kit, so an empty answer here means the
        read did not work rather than that there is nothing to play. Reading
        again costs a directory listing; being permanently wrong costs the
        feature.
        """
        if self._samples is None:
            found = list(sequencer_module.list_samples())
            if not found:
                return found
            self._samples = found
        return self._samples

    def forget(self, songs=False, kits=False, samples=False):
        """Drop what the card said, so the next read asks it again.

        Samples used to be exempt - nothing on the badge writes a .wav, so the
        list can only change by taking the card out - but that was an argument
        about staleness, not about cost. It is 12 KB, and holding it while a
        pattern plays is what the sampler cannot afford; see SettingsState.exit.
        """
        if songs:
            self._songs = None
        if kits:
            self._kits = None
        if samples:
            self._samples = None


class SettingsState(State):

    @property
    def name(self):
        return "settings"

    def __init__(self):
        self.catalog = Catalog()
        self.menu = Menu(settings.build(self.catalog), rows=MENU_ROWS)
        self._screen = screen_module.shared(display)

        self._entry = None  # naming something
        self._editor = None  # changing a number
        self._confirm = None  # (command, value) awaiting yes
        self._pending = None  # (command, value) awaiting a name
        self._message = None
        self._message_until = None
        # Which lines still need building, and the one to build next. Rows
        # are built one per pass to match the screen's own budget: building
        # all three costs 9.4 ms of a 32 ms audio buffer, and only one of
        # them can be pushed to the panel in that pass anyway.
        self._stale = 0b111
        self._next_line = 0
        # How far the selected label has slid, so the row is only rebuilt on
        # the passes where the text it would draw actually differs, and the
        # clock that was read for this pass. One reading, shared: marking the
        # row and building it must agree about the time, or the shift recorded
        # is not the shift drawn.
        self._scroll_shift = 0
        self._now = None
        self._last_select = 0
        self._last_volume = 0
        self._last_turn = None
        self._warmed = 0

    # --- the state machine ------------------------------------------------

    def enter(self, machine):
        # Whatever opened this screen is still in the queue as a press; the
        # matching release would otherwise arrive here and be acted on.
        keys.events.clear()
        # The cursor is left where it was. Coming back from the MIDI tool
        # should land on Tools rather than at the top, and a player changing
        # several track lengths should not have to walk down to Length each
        # time.
        #
        # The card is not re-read here either. It was read once during the
        # banner, and re-reading costs most of a second - so it happens only
        # after this badge has written to it, where the player is already
        # waiting for the write.
        self._entry = None
        self._editor = None
        self._confirm = None
        self._pending = None
        self._message = None
        self._message_until = None
        self._last_select = select_enc.position
        self._last_volume = volume_enc.position
        self._last_turn = None
        # Force the pads to be drawn once on entry, whatever they were left
        # showing by the screen before this one. Two fields rather than a
        # tuple of them: this is compared on every pass, and building a tuple
        # to compare was itself the allocation the comparison existed to
        # avoid. -1 is not a track, so the first pass always redraws.
        self._last_pad_track = -1
        self._last_pad_loaded = -1
        self._stale = 0b111
        self._next_line = 0
        self._scroll_shift = 0
        self._now = ticks_ms()
        # This state is built once and kept, so the menu still remembers a
        # slide from the last time the screen was open. Opening it again has
        # to show the start of a name, not the middle of one.
        self.menu.reset_scroll()
        self._screen.attach()
        # All three at once: a screen appearing a line at a time looks
        # broken, and entering a state is the one moment the stall is free.
        self._screen.set_lines(self._lines())
        self._screen.flush_all()
        self._stale = 0
        State.enter(self, machine)

    def update(self, machine):
        self._now = ticks_ms()
        self._read_encoders()
        if self._read_keys(machine):
            # The state changed. The next state owns the display now.
            return
        self._expire_message()
        self._render_pads()
        self._mark_scrolled()
        self._build_one_line()
        # Paced, one line per pass. This is what keeps a spun knob from
        # tearing the audio; see screen.py.
        self._screen.flush()

    def exit(self, machine):
        """Leave the panel dark.

        The pads mean "which track this row is about" only while this screen
        is up. Leaving one lit would read as the sampler's own state, which is
        the same reason FlashyState clears the strip on the way out.
        """
        neopixels.fill(view.OFF)
        neopixels.show()
        self._last_pad_track = -1
        self._last_pad_loaded = -1
        # And let go of every card-backed list, rows and listing alike. The
        # player is going back to the sampler, which needs the memory far more
        # than a menu nobody is looking at: the rows are 16 KB and the sample
        # listing behind them another 12, against 29 KB free without them.
        # See Menu.back for what holding them sounds like.
        for node in self._deferred():
            node.invalidate()
        self.catalog.forget(samples=True)
        State.exit(self, machine)

    # --- the pads ---------------------------------------------------------

    def _render_pads(self):
        """Light the pad of the track the current row is about.

        A setting that belongs to one track says so in words, on a row of 21
        columns, and the pads are already under the player's hand. Lighting
        the one that is about to change means the same glance that reads the
        row also checks the track - which is what stops a length or a sample
        landing on the wrong one.

        Dim on the tracks that have a sample, bright on the focused one, and
        dark everywhere when the row is not about a track at all. That is
        view.track_pads, the same picture Function-held draws on the sampler,
        so the two screens agree about what a lit pad means.

        The comparison is two integers, not the colour list, so the common
        case - nothing moved - allocates nothing. This runs on every pass of a
        loop that turns over thousands of times a second, and building a list
        of ten tuples each time is exactly the churn that turns into audible
        collections.
        """
        track = settings.focused_track(self.menu)
        loaded = 0
        for index in range(TRACK_COUNT):
            if sequencer.has_sample(index):
                loaded |= 1 << index
        if track == self._last_pad_track and loaded == self._last_pad_loaded:
            return
        self._last_pad_track = track
        self._last_pad_loaded = loaded
        # fill first, so the two indicator pixels are cleared as well: they
        # belong to the sampler and mean nothing here.
        neopixels.fill(view.OFF)
        if track is not None:
            colors = view.track_pads(
                track, [bool(loaded & (1 << i)) for i in range(TRACK_COUNT)]
            )
            for key_number in range(TRACK_COUNT):
                neopixels[neoindex(key_number)] = colors[key_number]
        neopixels.show()

    # --- input ------------------------------------------------------------

    def _read_encoders(self):
        # Volume first, and unconditionally: being unable to turn down a
        # sound that is too loud because a menu is open would be a poor
        # trade for the one screen that has nothing to do with volume.
        position = volume_enc.position
        if position != self._last_volume:
            sequencer.nudge_volume(position - self._last_volume, ticks_ms())
            self._last_volume = position
            self._show("Vol %d" % sequencer.volume_percent)

        position = select_enc.position
        if position == self._last_select:
            return
        delta = position - self._last_select
        self._last_select = position
        now = ticks_ms()
        elapsed = None
        if self._last_turn is not None:
            elapsed = ticks_diff(now, self._last_turn)
            if elapsed < 0:
                elapsed = None
        self._last_turn = now

        if self._entry is not None:
            self._entry.turn(delta)
        elif self._editor is not None:
            self._editor.turn(delta, elapsed)
        elif self._confirm is None:
            # A message is only ever informational, so a turn moves the menu
            # underneath it and puts the rows back.
            self._message = None
            self.menu.move(delta)
        self._all_stale()

    def _read_keys(self, machine):
        for _ in range(MAX_EVENTS_PER_PASS):
            event = keys.events.get()
            if not event:
                break
            if not event.pressed:
                continue
            if self._pressed(event.key_number, machine):
                return True
        return False

    def _pressed(self, key, machine):
        """Act on a press. Returns True if the state changed."""
        if key == VOLUME_KEY:
            # Not one of this screen's buttons.
            return False
        forward = key in (PLAY_KEY, SELECT_KEY)
        back = key == FUNCTION_KEY
        if not forward and not back:
            return False

        if self._entry is not None:
            return self._name_key(key)
        if self._editor is not None:
            return self._editor_key(forward)
        if self._confirm is not None:
            return self._confirm_key(forward)
        return self._browse_key(forward, machine)

    def _browse_key(self, forward, machine):
        self._message = None
        self._all_stale()
        if forward:
            item = self.menu.enter()
            if self.menu.last_error:
                # A list that would not fit. Said out loud rather than left
                # looking like an empty folder, which is what it did before.
                self._show("Out of memory")
            if item is not None:
                return self._open_item(item, machine)
            return False
        if not self.menu.back():
            machine.go_to_state("sampler")
            return True
        return False

    def _name_key(self, key):
        """Play finishes, the encoder's click sets a letter, Function rubs out.

        Play means yes everywhere else on the badge, so it means yes here
        too. Setting a letter is the click of the knob already being turned,
        which is a shorter reach than either.
        """
        if key == PLAY_KEY:
            self._entry.finish()
            self._finish_name()
        elif key == SELECT_KEY:
            if self._entry.accept():
                self._finish_name()
        elif not self._entry.backspace():
            # Backspacing past the first letter is how a name is abandoned;
            # there is nothing left to rub out, so it means "never mind".
            self._entry = None
            self._pending = None
            self._show("cancelled")
        self._all_stale()
        return False

    def _editor_key(self, forward):
        editor = self._editor
        self._editor = None
        if forward:
            if editor.label == "Bright":
                # The only editor whose value outlives the song, so it is the
                # only one with anywhere to be written.
                self._quietly(lambda: prefs.set_brightness(editor.value))
            self._show("%s %s" % (editor.label, editor.text))
        else:
            editor.cancel()
            self._show("cancelled")
        self._all_stale()
        return False

    def _confirm_key(self, forward):
        command, value = self._confirm
        self._confirm = None
        if forward:
            self._run_command(command, value)
        else:
            self._show("cancelled")
        self._all_stale()
        return False

    # --- commands ---------------------------------------------------------

    def _open_item(self, item, machine):
        """Decide what pressing a leaf means, and do it.

        Most rows need something on screen before anything happens - a name
        to type, a number to turn, a question to answer. The ones that do not
        fall through to _run_command, which is the half that actually acts.

        Returns True if the state changed.
        """
        command = item.command
        value = item.value

        if command == settings.TOOL_MIDI:
            machine.go_to_state("midi_controller")
            return True
        if command == settings.TOOL_HID:
            machine.go_to_state("hid")
            return True
        if command == settings.TRACK_FLASHY:
            machine.go_to_state("flashy")
            return True

        if command in (settings.SONG_DELETE, settings.KIT_DELETE):
            # The one action with nothing to undo it. There is no trash on
            # the badge, so it asks first.
            self._confirm = (command, value)
            return False

        if command == settings.TOOL_SCREENSAVER:
            self._ask_name(command, value, prefs.text())
            return False
        if command in (settings.SONG_SAVE_AS, settings.SONG_RENAME):
            self._ask_name(command, value, sequencer.song.name)
            return False
        if command in (settings.KIT_SAVE_AS, settings.KIT_RENAME):
            self._ask_name(command, value, sequencer.song.kit_name)
            return False
        if command == settings.SONG_SAVE and not sequencer.song.name:
            # Never saved, so there is no name to save over.
            self._ask_name(settings.SONG_SAVE_AS, value, "")
            return False
        if command == settings.KIT_SAVE and not sequencer.song.kit_name:
            self._ask_name(settings.KIT_SAVE_AS, value, "")
            return False

        if command == settings.TOOL_BRIGHTNESS:
            self._edit_brightness()
            return False
        if command == settings.TRACK_DIVISION:
            self._edit_division()
            return False
        if command == settings.LENGTH_GLOBAL:
            self._edit_length(None)
            return False
        if command == settings.LENGTH_TRACK:
            self._edit_length(value)
            return False

        self._run_command(command, value)
        return False

    def _ask_name(self, command, value, initial):
        # Rename starts from the current name, because it is usually a small
        # change to it. Save-as starts empty, because it is usually not.
        if command not in (
            settings.SONG_RENAME,
            settings.KIT_RENAME,
            settings.TOOL_SCREENSAVER,
        ):
            initial = ""
        self._entry = NameEntry(initial=initial or "")
        self._pending = (command, value)

    def _finish_name(self):
        name = self._entry.result()
        command, value = self._pending or (None, None)
        self._entry = None
        self._pending = None
        if not name:
            self._show("cancelled")
            return
        self._run_command(command, value, name)

    def _edit_division(self):
        """How long a step is. Set once when a pattern is started, which is
        why it lives here rather than on a modifier a thumb has to hold."""
        song = sequencer.song
        self._editor = Editor(
            "Div",
            song.division,
            0,
            len(DIVISIONS) - 1,
            apply=song.set_division,
            formatter=lambda index: DIVISIONS[index][0],
        )

    def _edit_length(self, track):
        song = sequencer.song
        if track is None:
            self._editor = Editor(
                "Length", song.length, MIN_LENGTH, MAX_STEPS, apply=song.set_length
            )
            return
        self._editor = Editor(
            "T%d" % (track + 1),
            song.track_length(track),
            MIN_LENGTH,
            MAX_STEPS,
            apply=lambda steps: song.set_track_length(track, steps),
        )

    def _edit_brightness(self):
        """Turn the knob, watch the panel. Committed to the card on accept.

        Applied as the knob turns because brightness is judged by looking at
        it, and written only on accept because a card write is most of a
        second and doing one per detent would be unusable.
        """
        self._editor = Editor(
            "Bright",
            int(round(neopixels.brightness * 100)),
            prefs.MIN_BRIGHTNESS,
            prefs.MAX_BRIGHTNESS,
            apply=_set_brightness,
            formatter=lambda percent: "%d%%" % percent,
        )

    def _run_command(self, command, value, name=None):
        song = sequencer.song
        if command == settings.SONG_SAVE:
            self._save_song(song.name)
        elif command == settings.SONG_SAVE_AS:
            self._save_song(name)
        elif command == settings.SONG_RENAME:
            self._rename(songfile, song.name, name, "song")
        elif command == settings.SONG_LOAD:
            self._load_song(value)
        elif command == settings.SONG_DELETE:
            self._delete(songfile, value, song, "name")

        elif command == settings.KIT_SAVE:
            self._save_kit(song.kit_name)
        elif command == settings.KIT_SAVE_AS:
            self._save_kit(name)
        elif command == settings.KIT_RENAME:
            self._rename(kitfile, song.kit_name, name, "kit")
        elif command == settings.KIT_LOAD:
            self._load_kit(value)
        elif command == settings.KIT_DELETE:
            self._delete(kitfile, value, song, "kit_name")

        elif command == settings.SAMPLE_TRACK:
            self._set_sample(value[0], value[1])
        elif command == settings.SAMPLE_CLEAR:
            self._set_sample(value, None)
        elif command == settings.TOOL_SCREENSAVER:
            self._quietly(lambda: prefs.set_text(name or ""))
            self._show("saved")
        elif command == settings.LIST_TRUNCATED:
            self._show("%d more on card" % value)
        else:
            self._show("not yet")

    # --- the card ---------------------------------------------------------

    def _quietly(self, work):
        """Run something that touches the card, with the voices stopped.

        Card operations block for hundreds of milliseconds - a listing is 500
        to 1000 ms on this hardware, and creating a directory measured eight
        seconds - against an audio buffer that holds 32. The buffer empties
        long before the call returns, and what the amplifier does with an
        empty buffer is repeat whatever was in it, which is a loud tearing
        noise.

        Stopping the voices first does not shorten the wait. It changes what
        the wait sounds like: an underrun of silence is silence, so the
        pattern cuts out for as long as the card takes and comes back in
        time. The clock is left running, so it comes back where it would
        have been rather than where it stopped.
        """
        for track in range(TRACK_COUNT):
            sequencer.silence_track(track)
        # And the watchdog held off, because some of these are slower than it
        # is: creating the songs directory on an empty card measured eight to
        # nine seconds, which is past even the largest timeout the RP2040
        # allows. See guard.py.
        return guard.slowly(work)

    # --- warming ----------------------------------------------------------

    def warm_step(self):
        """Do one slow card read. Returns False when there are none left.

        Called once per pass while the startup banner is up. Each of these
        measured most of a second on the badge and none of them can be
        afforded once a pattern is playing.

        What is warmed is the catalog, not the rows. The rows are built from
        it - eight track lists all read the same sample listing - so warming
        the three listings makes every row in the tree cheap to open, and
        warming the rows as well would only add their allocation to the boot
        for no gain.
        """
        if self._warmed >= len(_WARM_CARD):
            return False
        step = _WARM_CARD[self._warmed]
        self._warmed += 1
        try:
            # Warming runs inside the main loop, so the watchdog is already
            # armed by the time the banner starts reading the card.
            guard.slowly(lambda: step(self))
        except OSError:
            # No card, or a card that will not answer. Not fatal: the badge
            # plays without one, and the rows that need it will say so.
            pass
        return True

    def _deferred(self):
        found = []
        _collect(self.menu.root, found)
        return found

    # --- songs and kits ---------------------------------------------------

    def _save_song(self, name):
        song = sequencer.song
        try:
            self._quietly(lambda: songfile.save(song, name))
        except StoreError as error:
            self._fail(error)
            return
        song.name = name
        self._remember_song(name)
        self._forget_listings(songs=True)
        self._show("saved %s" % name)

    def _save_kit(self, name):
        song = sequencer.song
        # Whatever is currently sounding becomes this kit's baseline, so
        # balancing a kit by ear and saving it is the whole workflow.
        volumes = song.capture_kit_volumes()
        try:
            self._quietly(lambda: kitfile.save(song.kit, name, volumes))
        except StoreError as error:
            self._fail(error)
            return
        song.kit_name = name
        self._remember_kit()
        self._forget_listings(kits=True)
        self._show("saved %s" % name)

    def _rename(self, module, old, new, kind):
        if not old:
            # Nothing on the card yet, so this is a save under a new name.
            if kind == "song":
                self._save_song(new)
            else:
                self._save_kit(new)
            return
        try:
            self._quietly(lambda: module.rename(old, new))
        except StoreError as error:
            self._fail(error)
            return
        if kind == "song":
            sequencer.song.name = new
            # The old name is gone from the card and prefs still points at it.
            # Left alone, the next boot looks for a song that no longer exists,
            # quietly falls back to the demo, and the badge forgets what it was
            # playing - but only ever after a rename, which is exactly the kind
            # of bug nobody connects to the thing they did.
            self._remember_song(new)
        else:
            sequencer.song.kit_name = new
        self._forget_listings(songs=kind == "song", kits=kind != "song")
        self._show("renamed")

    def _load_song(self, name):
        try:
            song = self._quietly(lambda: songfile.load(name))
        except StoreError as error:
            self._fail(error)
            return
        song.name = name
        loaded = sequencer.load_song(song)
        self._remember_song(name)
        # The song brought its own kit, so that is now the setup in use.
        self._remember_kit()
        # Out of the list of songs: it has been chosen, and staying in it
        # invites choosing another by accident.
        self.menu.back()
        self._show("%s %d/%d" % (name, loaded, TRACK_COUNT))

    def _load_kit(self, name):
        try:
            paths = self._quietly(lambda: kitfile.load(name))
        except StoreError as error:
            self._fail(error)
            return
        song = sequencer.song
        volumes = self._quietly(lambda: kitfile.load_volumes(name))
        for track in range(TRACK_COUNT):
            song.set_sample(track, paths[track])
            song.set_kit_volume(track, volumes[track])
        song.kit_name = name
        loaded = sequencer.load_kit(paths)
        self._remember_kit()
        self.menu.back()
        self._show("%s %d/%d" % (name, loaded, TRACK_COUNT))

    def _delete(self, module, name, song, attribute):
        removed = self._quietly(lambda: module.delete(name))
        if removed and getattr(song, attribute) == name:
            # The badge is no longer holding something that exists on the
            # card, so Save has to ask for a name rather than write it back.
            setattr(song, attribute, None)
        # Out of the listing first: it is about to be forgotten, and being
        # left standing in a branch with no rows reads as an empty card.
        self.menu.back()
        self._forget_listings(songs=attribute == "name", kits=attribute != "name")
        self._show("deleted" if removed else "not there")

    def _set_sample(self, track, path):
        song = sequencer.song
        # Out of the listing first, exactly as _delete does, and for a harder
        # reason: the rows of a 98-sample list are about 16 KB, which is most
        # of what is free, and assigning may have to reload the whole kit to
        # make room for this track. Measured on the badge with the list still
        # open: 6,256 bytes free, and the reshare could not allocate. Leaving
        # first gives it back about 22 KB and lands the player on the row for
        # the track they just set, which is where they can hear it.
        self.menu.back()
        if path is None:
            sequencer.assign_sample(track, None)
            self._remember_kit()
            self._show("T%d cleared" % (track + 1))
            return
        # assign_sample, not load_track: a kit spends the whole sample budget,
        # so a track that had nothing has nothing left to spend. It reshares
        # rather than refusing - see sequencer.assign_sample.
        if self._quietly(lambda: sequencer.assign_sample(track, path)):
            # Audible confirmation, which on a badge with eight identical
            # pads is worth more than the words.
            sequencer.trigger(track, 100)
            self._remember_kit()
            self._show("T%d %s" % (track + 1, _short(path)))
        else:
            song.set_sample(track, None)
            # The reason if there is one. "failed" alone sent the player
            # looking for a bad file when the answer was that the badge had
            # run out of room.
            reason = sequencer.last_error
            if reason and "budget" in reason:
                self._show("T%d no room" % (track + 1))
            else:
                self._show("T%d failed" % (track + 1))

    def _remember_song(self, name):
        """Note which song the badge is on, so a power cycle comes back to it.

        Best effort, and quiet about failing: a badge with no card forgets
        between power-ups, which is the same deal the rest of prefs makes.
        """
        try:
            self._quietly(lambda: prefs.set_last_song(name))
        except Exception:
            # Deliberately everything. This is a note to self on a card that
            # may be absent, full, or slow, and it runs on the way out of a
            # save the player has already been told succeeded. Failing it must
            # not take the badge down.
            pass

    def _remember_kit(self):
        """Note the samples in use, for the same reason.

        Recorded whenever they change rather than only when a kit is saved:
        the setup a player is actually using is usually the one they swapped
        together, not one they gave a name to.
        """
        try:
            self._quietly(lambda: prefs.set_last_kit(sequencer.song.kit))
        except Exception:
            # Same: see _remember_song. Building the list of paths allocates,
            # and this runs at the moment the heap is most spoken for.
            pass

    def _forget_listings(self, songs=False, kits=False):
        """Make the next open of Load or Delete read the card again.

        Only after this badge has written to it, and only the listing that
        changed: re-reading is most of a second, so doing it speculatively
        is the difference between a menu that opens and one that stalls.

        Which rows those are is asked of the rows themselves - each deferred
        branch carries the listing it was built from. Finding them by their
        position in the tree instead would break silently the first time a
        section was reordered in engine/settings.py.
        """
        self.catalog.forget(songs=songs, kits=kits)
        wanted = []
        if songs:
            wanted.append("songs")
        if kits:
            wanted.append("kits")
        for node in self._deferred():
            if node.kind in wanted:
                node.invalidate()
        # The cursor may be standing in one of the lists just forgotten. The
        # callers all leave it first, but that is a courtesy to the player
        # rather than something this can rely on, and a branch with no rows
        # reads as an empty card.
        self.menu.refresh()

    # --- messages ---------------------------------------------------------

    def _all_stale(self):
        self._stale = (1 << len(self._screen)) - 1

    def _show(self, text):
        self._message = text
        self._message_until = ticks_ms() + MESSAGE_MS
        self._all_stale()

    def _fail(self, error):
        # The message carries the path and the errno, which will not fit.
        # The first words are the part that says what went wrong.
        self._show(str(error)[:WIDTH])

    def _expire_message(self):
        if self._message_until is None:
            return
        if ticks_diff(ticks_ms(), self._message_until) >= 0:
            self._message = None
            self._message_until = None
            self._all_stale()

    # --- drawing ----------------------------------------------------------

    def _build_one_line(self):
        """Rebuild at most one stale line. See _stale."""
        if not self._stale:
            return
        for _ in range(len(self._screen)):
            index = self._next_line
            self._next_line = (index + 1) % len(self._screen)
            if self._stale & (1 << index):
                self._stale &= ~(1 << index)
                self._screen.set_line(index, self._line(index))
                return

    def _line(self, index):
        """One line of whichever screen is showing."""
        if self._entry is not None:
            if index == 0:
                return "Name:      Play=ok"
            if index == 1:
                return self._entry.preview[:WIDTH]
            return "[%s] Sel=add Fn=del" % self._entry.letter_label
        if self._editor is not None:
            if index == 0:
                return self.menu.breadcrumb(WIDTH)
            if index == 1:
                return self._editor.label + ": " + self._editor.text
            return "A=keep  B=cancel"
        if self._confirm is not None:
            if index == 0:
                return CONFIRM_VERBS.get(self._confirm[0], "Are you sure?")
            if index == 1:
                return str(self._confirm[1])[:WIDTH]
            return "A=yes  B=no"
        if self._message is not None:
            if index == 0:
                return self.menu.breadcrumb(WIDTH)
            return self._message[:WIDTH] if index == 1 else ""
        if index == 0:
            return self._heading()
        return self.menu.row(index - 1, WIDTH, self._now)

    @property
    def _overlay(self):
        """Whether anything is covering the menu.

        One definition, because more than one place asks. _line dispatches on
        which overlay is up, since it has to draw that one; this only needs to
        know that the menu is not on screen, and a row nobody can see must not
        be marked for redrawing.
        """
        return (
            self._entry is not None
            or self._editor is not None
            or self._confirm is not None
            or self._message is not None
        )

    def _mark_scrolled(self):
        """Dirty the selected row when its label has slid another character.

        Only the row under the cursor ever moves, and only while its name is
        too long for the width, so this asks the menu for the shift and marks
        one line - about five times a second while something is sliding, and
        never at all otherwise. Cheaper than dirtying the row every pass and
        letting the screen work out that the text is unchanged, because
        building a row is the expensive half.

        Uses the pass's own clock reading rather than taking its own, so the
        shift recorded here is exactly the one _line will draw.
        """
        if self._overlay:
            return
        shift = self.menu.scroll_shift(self._now, WIDTH)
        if shift == self._scroll_shift:
            return
        self._scroll_shift = shift
        # Line 0 is the heading, so the menu's first row is line 1.
        line = self.menu.cursor - self.menu.offset + 1
        if 0 < line < len(self._screen):
            self._stale |= 1 << line

    def _lines(self):
        """Every line at once. For entering the screen, and for tests."""
        return [self._line(index) for index in range(len(self._screen))]

    def _heading(self):
        """Where you are, and how far down a list that does not fit."""
        position, total = self.menu.position
        if total <= MENU_ROWS:
            return self.menu.breadcrumb(WIDTH)
        count = " %d/%d" % (position, total)
        return "%-*s%s" % (
            WIDTH - len(count),
            self.menu.breadcrumb(WIDTH - len(count)),
            count,
        )


# One slow card operation each, done a pass apart while the banner is up.
# Creating the directories is here rather than at the first save because on
# an empty card that measured eight seconds.
_WARM_CARD = (
    # First, because it is the one the player sees immediately: the panel
    # comes up at the built-in default and this is what corrects it.
    lambda state: _set_brightness(prefs.brightness()),
    lambda state: state.catalog.songs(),
    lambda state: state.catalog.kits(),
    # The samples are deliberately NOT warmed here. Measured on the badge, the
    # listing of 98 of them is about 12 KB of names and paths - and warming it
    # at boot meant holding that for the whole session, which left 17 KB free
    # against main.py's 16 KB collection floor. The loop then collected on
    # almost every pass, 25 ms at a time into a 32 ms audio buffer, and a
    # pattern played its samples only sometimes.
    #
    # Read on the first sample list opened instead, and dropped again when the
    # screen closes. That puts the stall where a player has just asked for
    # something and takes it off the thing they are listening to. Songs and
    # kits stay: a handful of short names is nothing like the same cost.
    lambda state: songfile.available(),
    lambda state: kitfile.available(),
)


def _set_brightness(percent):
    neopixels.brightness = percent / 100.0
    neopixels.show()


def _collect(item, found):
    """Every deferred branch in the tree, in the order they are shown."""
    if item.builder is not None:
        found.append(item)
    for child in item.children or ():
        _collect(child, found)


def _short(path):
    """Just the filename, which is all a 21 column screen has room for."""
    if not path:
        return "-"
    return path.rsplit("/", 1)[-1]
