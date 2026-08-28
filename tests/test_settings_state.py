"""Tests for the settings screen.

Drives the state the way the main loop does - post key events, turn the
encoders, call update - and checks what happened to the engine and to the
card. The stubs make that possible off-hardware.

Three things matter here beyond "the right thing happened". The screen is
opened while a pattern is playing, so it must not do a card's worth of work
on the way in, and it must not draw more than one line in a pass. And the
buttons mean something different from what they mean in the sampler, which
is the whole reason this state exists.
"""

import struct

import pytest

import circuitpython_stubs
from conftest import FakeMachine
import kitfile
import prefs
from engine.naming import ALPHABET
import screen as screen_module
import sequencer as sequencer_module
import setup
import songfile
import SettingsState as settings_module
from engine.song import MAX_STEPS, TRACK_COUNT
from SettingsState import FUNCTION_KEY, PLAY_KEY, SELECT_KEY, SettingsState

A = PLAY_KEY  # enter / yes / keep
B = FUNCTION_KEY  # back / no / cancel


def write_wav(path, frames=64):
    rate = sequencer_module.SAMPLE_RATE
    data = struct.pack("<%dh" % frames, *([0] * frames))
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    path.write_bytes(header + data)
    return str(path)


@pytest.fixture
def card(tmp_path, monkeypatch):
    """A card with songs, kits and samples on it."""
    for name in ("songs", "kits", "samples"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(songfile.store, "directory", str(tmp_path / "songs"))
    monkeypatch.setattr(kitfile.store, "directory", str(tmp_path / "kits"))
    monkeypatch.setattr(prefs.store, "directory", str(tmp_path))
    paths = [write_wav(tmp_path / "samples" / ("s%d.wav" % n)) for n in range(3)]
    monkeypatch.setattr(sequencer_module, "SAMPLE_DIRS", (str(tmp_path / "samples"),))
    return paths


@pytest.fixture
def engine(tmp_path):
    engine = sequencer_module.engine
    engine.transport.stop()
    engine.clock.stop()
    engine.clock.reset()
    engine.song.clear_all()
    engine.song.set_length(16)
    engine.song.name = None
    engine.song.kit_name = None
    for track in range(TRACK_COUNT):
        engine.load_track(track, None)
        engine.song.set_sample(track, None)
    engine.set_volume_position(24)
    return engine


@pytest.fixture
def state(engine, card):
    setup.keys.events.clear()
    setup.select_enc.position = 0
    setup.volume_enc.position = 0
    s = SettingsState()
    s.enter(FakeMachine())
    return s


def press(key):
    setup.keys.events.post(circuitpython_stubs.Event(key, pressed=True))


def release(key):
    setup.keys.events.post(circuitpython_stubs.Event(key, pressed=False))


def run(state, machine=None, passes=8):
    """Pump the state the way the main loop does."""
    machine = machine or FakeMachine()
    for _ in range(passes):
        circuitpython_stubs.ticks.value += 1
        state.update(machine)
    return machine


def turn(state, steps, machine=None):
    setup.select_enc.position += steps
    return run(state, machine)


def go(state, *labels):
    """Walk down to a row by name, entering each one."""
    machine = FakeMachine()
    for label in labels:
        rows = [item.label for item in state.menu.items]
        assert label in rows, "%s is not in %s" % (label, rows)
        turn(state, rows.index(label) - state.menu.cursor, machine)
        press(A)
        run(state, machine)
    return machine


# --- the buttons ----------------------------------------------------------


def test_play_goes_into_a_submenu(state):
    press(A)
    run(state)
    assert state.menu.depth == 1


def test_the_encoder_click_also_goes_in(state):
    """It is the button that opened the screen; it should keep meaning yes."""
    press(SELECT_KEY)
    run(state)
    assert state.menu.depth == 1


def test_function_comes_back_out(state):
    press(A)
    run(state)
    press(B)
    run(state)
    assert state.menu.depth == 0


def test_function_at_the_top_returns_to_the_sampler(state):
    machine = FakeMachine()
    press(B)
    run(state, machine)
    assert machine.transitions == ["sampler"]


def test_function_at_the_top_does_not_leave_from_inside_a_submenu(state):
    machine = FakeMachine()
    press(A)
    run(state, machine)
    press(B)
    run(state, machine)
    assert machine.transitions == [], "one press left the screen from two levels in"


def test_play_does_not_start_the_transport(state):
    """Inside settings it means yes, not play."""
    press(A)
    release(A)
    run(state)
    assert sequencer_module.engine.transport.playing is False


def test_function_does_not_change_the_sampler_view(state):
    before = sequencer_module.engine.mode
    press(B)
    release(B)
    run(state)
    assert sequencer_module.engine.mode == before


def test_the_press_that_opened_the_screen_is_not_acted_on_again(engine, card):
    """Its release is still in the queue when this state is entered."""
    setup.keys.events.clear()
    press(SELECT_KEY)
    s = SettingsState()
    machine = FakeMachine()
    s.enter(machine)
    run(s, machine)
    assert s.menu.depth == 0
    assert machine.transitions == []


# --- the knobs ------------------------------------------------------------


def test_turning_the_encoder_moves_the_cursor(state):
    turn(state, 1)
    assert state.menu.cursor == 1


def test_the_cursor_stops_at_the_end_rather_than_wrapping(state):
    turn(state, 50)
    assert state.menu.cursor == len(state.menu.items) - 1


def test_the_volume_knob_still_works_while_the_menu_is_open(state):
    """Being unable to turn a loud sound down because a menu is open is bad."""
    before = sequencer_module.engine.volume_position
    setup.volume_enc.position += 1
    run(state)
    assert sequencer_module.engine.volume_position > before


def test_turning_the_volume_knob_says_what_it_did(state):
    setup.volume_enc.position += 1
    run(state)
    assert "Vol" in state._screen.line(1)


# --- lengths --------------------------------------------------------------


def test_a_track_length_can_be_edited(state):
    go(state, "Track", "Length", "Track 3")
    turn(state, 4)
    assert sequencer_module.engine.song.track_length(2) > 16


def test_the_length_changes_as_the_knob_turns_rather_than_on_commit(state):
    """A pattern length is judged by listening to it."""
    go(state, "Track", "Length", "Global")
    turn(state, 2)
    assert sequencer_module.engine.song.length != 16


def test_cancelling_a_length_edit_puts_the_old_one_back(state):
    go(state, "Track", "Length", "Global")
    turn(state, 5)
    press(B)
    run(state)
    assert sequencer_module.engine.song.length == 16


def test_keeping_a_length_edit_leaves_the_new_one(state):
    go(state, "Track", "Length", "Global")
    turn(state, 5)
    changed = sequencer_module.engine.song.length
    press(A)
    run(state)
    assert sequencer_module.engine.song.length == changed


def test_a_length_cannot_be_pushed_past_the_pattern_buffer(state):
    go(state, "Track", "Length", "Global")
    turn(state, 500)
    assert sequencer_module.engine.song.length == MAX_STEPS


def test_leaving_the_editor_returns_to_the_rows(state):
    go(state, "Track", "Length", "Global")
    press(A)
    run(state)
    assert state._editor is None


# --- songs ----------------------------------------------------------------


def name_it(state, text):
    """Spell a name the way the badge does it.

    Turn to a letter, click the encoder to set it, and press Play when the
    name is right. Play is yes here as it is everywhere else; the click of
    the knob already being turned is what sets a character.
    """
    machine = FakeMachine()
    for letter in text:
        target = ALPHABET.index(letter)
        turn(state, target - ALPHABET.index(state._entry.letter), machine)
        press(SELECT_KEY)
        run(state, machine)
    press(A)
    run(state, machine)
    return machine


def test_saving_a_song_that_has_no_name_yet_asks_for_one(state):
    go(state, "Song", "Save")
    assert state._entry is not None


def test_a_named_song_is_written_to_the_card(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    assert songfile.songs() == ["BEAT"]
    assert sequencer_module.engine.song.name == "BEAT"


def test_saving_again_does_not_ask_for_the_name_a_second_time(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Save")
    assert state._entry is None
    assert songfile.songs() == ["BEAT"]


def test_a_saved_song_can_be_loaded_back(state):
    song = sequencer_module.engine.song
    song.set_step(0, 3, 100)
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    song.clear_all()
    assert song.is_empty()
    go(state, "Load", "BEAT")
    assert sequencer_module.engine.song.velocity(0, 3) == 100


def test_loading_a_song_leaves_the_list_it_was_chosen_from(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Load", "BEAT")
    assert [item.label for item in state.menu.items] == [
        "Save",
        "Save as",
        "Rename",
        "Load",
        "Delete",
    ]


def test_loading_a_song_stops_the_transport(state):
    """The tracks are about to point at other samples."""
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    sequencer_module.engine.transport.toggle_play()
    go(state, "Load", "BEAT")
    assert sequencer_module.engine.transport.playing is False


def test_deleting_a_song_asks_first(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Delete", "BEAT")
    assert state._confirm is not None
    assert songfile.songs() == ["BEAT"], "it was deleted before being confirmed"


def test_confirming_removes_it(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Delete", "BEAT")
    press(A)
    run(state)
    assert songfile.songs() == []


def test_refusing_keeps_it(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Delete", "BEAT")
    press(B)
    run(state)
    assert songfile.songs() == ["BEAT"]


def test_deleting_the_song_that_is_loaded_forgets_its_name(state):
    """Save would otherwise write it back without asking, recreating it."""
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Delete", "BEAT")
    press(A)
    run(state)
    assert sequencer_module.engine.song.name is None


def test_a_song_saved_since_the_screen_last_opened_appears_in_the_list(state):
    """The listing is cached, so entering has to forget it."""
    songfile.save(sequencer_module.engine.song, "ELSEWHERE")
    state.enter(FakeMachine())
    go(state, "Song", "Load")
    assert [item.label for item in state.menu.items] == ["ELSEWHERE"]


def test_an_empty_card_offers_a_row_saying_so_rather_than_nothing(state):
    go(state, "Song", "Load")
    assert state.menu.rendered()[0] == "(none)"


def test_backing_out_of_an_empty_list_still_works(state):
    go(state, "Song", "Load")
    press(B)
    run(state)
    assert state.menu.depth == 1


# --- kits and samples -----------------------------------------------------


def test_a_sample_can_be_assigned_to_a_track(state, card):
    go(state, "Samples", "Tracks", "Track 2")
    rows = [item.label for item in state.menu.items]
    assert "s0.wav" in rows
    go(state, "s0.wav")
    assert sequencer_module.engine.song.kit[1] == card[0]
    assert sequencer_module.engine.has_sample(1) is True


def test_a_track_can_be_emptied_again(state, card):
    go(state, "Samples", "Tracks", "Track 2", "s0.wav")
    go(state, "(none)")
    assert sequencer_module.engine.song.kit[1] is None
    assert sequencer_module.engine.has_sample(1) is False


def test_a_kit_survives_being_saved_and_loaded(state, card):
    go(state, "Samples", "Tracks", "Track 2", "s0.wav")
    press(B)  # out of the sample list
    run(state)
    press(B)  # out of Tracks
    run(state)
    go(state, "Kit", "Save")
    name_it(state, "KIT")
    assert kitfile.kits() == ["KIT"]
    sequencer_module.engine.song.set_sample(1, None)
    go(state, "Load", "KIT")
    assert sequencer_module.engine.song.kit[1] == card[0]


# --- the audio budget -----------------------------------------------------


def test_opening_the_screen_does_not_read_the_card(engine, card, monkeypatch):
    """A directory listing is tens of milliseconds against a 32 ms buffer."""
    reads = []
    real = songfile.songs
    monkeypatch.setattr(
        settings_module.songfile, "songs", lambda: (reads.append(1), real())[1]
    )
    setup.keys.events.clear()
    s = SettingsState()
    s.enter(FakeMachine())
    assert reads == [], "building the tree listed the card"


def test_the_card_is_read_when_the_row_is_opened(state, monkeypatch):
    reads = []
    real = songfile.songs
    monkeypatch.setattr(
        settings_module.songfile, "songs", lambda: (reads.append(1), real())[1]
    )
    go(state, "Song", "Load")
    assert reads == [1]


def test_scrolling_never_draws_more_than_one_line_in_a_pass(state):
    """Two lines is 19 ms, and the audio buffer holds 32."""
    shared = screen_module.shared(setup.display)
    machine = FakeMachine()
    for step in range(40):
        setup.select_enc.position += 1
        # Well past the flush interval, so drawing is never skipped for
        # being too soon - which would hide the thing being tested.
        circuitpython_stubs.ticks.value += 100
        before = [shared.drawn(i) for i in range(len(shared))]
        state.update(machine)
        after = [shared.drawn(i) for i in range(len(shared))]
        changed = sum(1 for i in range(len(shared)) if before[i] != after[i])
        assert changed <= 1, "drew %d lines in one pass" % changed


def test_the_screen_catches_up_with_the_knob(state):
    """Paced drawing must converge, not fall permanently behind."""
    shared = screen_module.shared(setup.display)
    turn(state, 3)
    for _ in range(60):
        circuitpython_stubs.ticks.value += 100
        state.update(FakeMachine())
    assert shared.pending == 0


def test_entering_the_screen_shows_all_of_it_at_once(state):
    """Revealing a line per pass looks broken; entry is when a stall is free."""
    state.enter(FakeMachine())
    assert screen_module.shared(setup.display).pending == 0


def test_the_settings_screen_takes_the_display(state):
    shared = screen_module.shared(setup.display)
    state.enter(FakeMachine())
    assert setup.display.shown is shared.group


def test_a_backlog_of_presses_is_not_all_handled_in_one_pass(state):
    """Unbounded draining is the same failure the redraw budget prevents."""
    for _ in range(6):
        press(A)
    state.update(FakeMachine())
    assert state.menu.depth <= settings_module.MAX_EVENTS_PER_PASS


# --- what is on screen ----------------------------------------------------


def test_the_top_line_says_where_you_are(state):
    go(state, "Song")
    assert state._lines()[0].startswith("Song")


def test_a_list_longer_than_the_screen_says_how_far_down_it_is(state):
    go(state, "Song")
    assert "1/5" in state._lines()[0]


def test_the_rows_fit_the_display(state):
    """terminalio on a 128px panel is about 21 characters."""
    go(state, "Samples", "Tracks")
    for line in state._lines():
        assert len(line) <= 21, line


def test_the_naming_screen_fits_the_display(state):
    go(state, "Song", "Save as")
    for line in state._lines():
        assert len(line) <= 21, line


def test_every_line_is_printable_on_the_builtin_font(state):
    """terminalio is ASCII; anything else draws as a blank box."""
    go(state, "Song", "Save as")
    for line in state._lines():
        for character in line:
            assert 32 <= ord(character) < 127, repr(line)


def test_the_startup_screen_imports_the_settings_modules():
    """1.3 s of compiling off the card must not land on a playing pattern.

    The modules are put back afterwards. Dropping one and letting it be
    imported again leaves a second copy of it: whatever was already holding
    the first - a fixture that patched its store, say - is then talking to a
    different object than the code under test, and the two disagree silently.
    """
    import sys

    from StartupState import WARM, StartupState

    saved = {name: sys.modules.get(name) for name in WARM}
    try:
        for name in WARM:
            sys.modules.pop(name, None)
        state = StartupState()
        state.warmed = 0
        state._warm(FakeMachine())
        for name in WARM:
            assert name in sys.modules, "%s is still uncompiled" % name
    finally:
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module


def test_the_banner_keeps_animating_while_the_badge_warms():
    """A badge whose lights stop looks like one that has hung."""
    from StartupState import StartupState

    machine = FakeMachine()
    state = StartupState()
    state.enter(machine)
    state.warmed = 0
    before = state.timer
    state.update(machine)
    assert state.timer > before, "the animation stopped for the warm-up"


def test_warming_imports_one_module_per_pass():
    """Doing the lot in one call freezes the banner for twelve seconds."""
    import sys

    from StartupState import WARM, StartupState

    saved = {name: sys.modules.get(name) for name in WARM}
    try:
        for name in WARM:
            sys.modules.pop(name, None)
        state = StartupState()
        state.warmed = 0
        assert state._warm_step(FakeMachine()) is True
        assert state.warmed == 1
        assert sys.modules.get(WARM[1]) is None, "it imported more than one"
    finally:
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module


def test_warming_stops_when_there_is_nothing_left():
    from StartupState import WARM, StartupState

    state = StartupState()
    state.warmed = len(WARM)
    machine = FakeMachine()
    settings_state = machine.state_for("settings")
    while settings_state.warm_step():
        pass
    assert state._warm_step(machine) is False


def test_a_module_that_will_not_import_does_not_stall_the_banner(monkeypatch):
    """It fails again where it is needed, with a screen up to say so."""
    from StartupState import StartupState

    monkeypatch.setattr("StartupState.WARM", ("nonexistent_module_xyz",))
    state = StartupState()
    state.warmed = 0
    state._warm(FakeMachine())
    assert state.warmed == 1


def test_a_line_is_built_only_when_it_is_stale(state):
    """Building three rows is 9.4 ms of a 32 ms buffer, to draw one."""
    built = []
    real = state._line
    state._line = lambda index: (built.append(index), real(index))[1]
    turn(state, 1, FakeMachine())
    assert len(built) <= len(state._screen), "rebuilt the screen more than once"


def test_a_settled_screen_builds_nothing_at_all(state):
    run(state, passes=20)
    built = []
    real = state._line
    state._line = lambda index: (built.append(index), real(index))[1]
    run(state, passes=20)
    assert built == []


# --- the card is slow -----------------------------------------------------
#
# Measured on the badge: a directory listing over SPI is 500 to 1000 ms and
# creating one measured eight seconds, against an audio buffer that holds 32
# milliseconds. Every one of these tests exists because of those numbers.


def test_a_listing_is_read_once_and_remembered(state, monkeypatch):
    reads = []
    real = songfile.songs
    monkeypatch.setattr(
        settings_module.songfile, "songs", lambda: (reads.append(1), real())[1]
    )
    state.catalog.forget(songs=True)
    state.catalog.songs()
    state.catalog.songs()
    assert reads == [1]


def test_the_sample_list_is_not_read_again_for_every_track(state, monkeypatch):
    """Eight track rows share one list; reading it eight times is eight seconds."""
    reads = []
    real = sequencer_module.list_samples
    monkeypatch.setattr(
        settings_module.sequencer_module,
        "list_samples",
        lambda: (reads.append(1), real())[1],
    )
    state.catalog._samples = None
    go(state, "Samples", "Tracks", "Track 1")
    press(B)
    run(state)
    go(state, "Track 2")
    assert reads == [1]


def test_reopening_the_screen_does_not_re_read_the_card(state, monkeypatch):
    """It was read during the banner; a second is not affordable here."""
    go(state, "Song", "Load")
    reads = []
    real = songfile.songs
    monkeypatch.setattr(
        settings_module.songfile, "songs", lambda: (reads.append(1), real())[1]
    )
    state.enter(FakeMachine())
    go(state, "Load") if state.menu.depth == 1 else None
    assert reads == []


def test_saving_makes_the_song_list_read_again(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Load")
    assert [item.label for item in state.menu.items] == ["BEAT"]


def test_deleting_makes_the_song_list_read_again(state):
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    go(state, "Delete", "BEAT")
    press(A)
    run(state)
    go(state, "Load")
    assert state.menu.rendered()[0] == "(none)"


def test_saving_a_song_does_not_disturb_the_kit_list(state):
    """Re-reading what did not change is a second of torn audio for nothing."""
    kitfile.save([None] * TRACK_COUNT, "KIT")
    state.catalog.forget(kits=True)
    state.catalog.kits()
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    assert state.catalog._kits is not None, "the kit list was dropped for nothing"


def test_the_voices_are_stopped_before_the_card_is_touched(state, monkeypatch):
    """An underrun of silence is silence; an underrun of a note is a tear."""
    order = []
    real_silence = sequencer_module.engine.silence_track
    monkeypatch.setattr(
        sequencer_module.engine,
        "silence_track",
        lambda track: (order.append("silence"), real_silence(track))[1],
    )
    real_save = songfile.store.save
    monkeypatch.setattr(
        songfile.store,
        "save",
        lambda data, name: (order.append("card"), real_save(data, name))[1],
    )
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    assert "card" in order
    assert order.index("silence") < order.index("card")


def test_every_track_is_silenced_not_just_the_playing_one(state, monkeypatch):
    silenced = []
    real = sequencer_module.engine.silence_track
    monkeypatch.setattr(
        sequencer_module.engine,
        "silence_track",
        lambda track: (silenced.append(track), real(track))[1],
    )
    go(state, "Song", "Save")
    name_it(state, "BEAT")
    assert sorted(set(silenced)) == list(range(TRACK_COUNT))


def test_warming_reads_every_listing(state):
    state.catalog._songs = state.catalog._kits = state.catalog._samples = None
    state._warmed = 0
    steps = 0
    while state.warm_step():
        steps += 1
        assert steps < 100, "warming does not finish"
    assert state.catalog._songs is not None
    assert state.catalog._kits is not None
    assert state.catalog._samples is not None


def test_warming_does_one_slow_thing_per_call(state):
    """One per call, whatever it is. Brightness comes first, then the card."""
    state.catalog._songs = state.catalog._kits = None
    state._warmed = 0
    state.warm_step()  # brightness
    assert state.catalog._songs is None, "it did two things in one call"
    state.warm_step()  # songs
    assert state.catalog._songs is not None
    assert state.catalog._kits is None, "it read more than one listing"


def test_warming_leaves_the_rows_to_be_built_when_opened(state):
    """Eight track lists all read one listing; warming them adds nothing."""
    state._warmed = 0
    while state.warm_step():
        pass
    assert not any(node.built for node in state._deferred())


def test_warming_creates_the_directories_before_the_first_save(state, tmp_path):
    """Creating one measured eight seconds on an empty card."""
    import os

    fresh = tmp_path / "fresh"
    songfile.store.directory = str(fresh / "songs")
    kitfile.store.directory = str(fresh / "kits")
    fresh.mkdir()
    state._warmed = 0
    while state.warm_step():
        pass
    assert os.path.isdir(songfile.store.directory)
    assert os.path.isdir(kitfile.store.directory)


def test_a_card_that_will_not_answer_does_not_stop_the_banner(state, monkeypatch):
    def explode():
        raise OSError("no card")

    monkeypatch.setattr(settings_module.songfile, "songs", explode)
    state.catalog._songs = None
    state._warmed = 0
    steps = 0
    while state.warm_step():
        steps += 1
        assert steps < 100
    assert steps == len(settings_module._WARM_CARD)


# --- brightness ------------------------------------------------------------


def test_brightness_lives_under_tools(state):
    go(state, "Tools")
    assert "Brightness" in [item.label for item in state.menu.items]


def test_opening_brightness_gives_a_number_to_turn(state):
    go(state, "Tools", "Brightness")
    assert state._editor is not None
    assert state._editor.label == "Bright"


def test_the_panel_changes_as_the_knob_turns(state):
    """It is judged by looking at it, so it applies live rather than on accept."""
    go(state, "Tools", "Brightness")
    before = setup.neopixels.brightness
    turn(state, 4)
    assert setup.neopixels.brightness != before


def test_cancelling_puts_the_old_brightness_back(state):
    go(state, "Tools", "Brightness")
    before = setup.neopixels.brightness
    turn(state, 5)
    press(B)
    run(state)
    assert setup.neopixels.brightness == pytest.approx(before)


def test_accepting_writes_it_to_the_card(state):
    import prefs

    go(state, "Tools", "Brightness")
    turn(state, 5)
    chosen = state._editor.value
    press(A)
    run(state)
    assert prefs.brightness() == chosen


def test_cancelling_writes_nothing(state):
    import prefs

    prefs.set_brightness(12)
    go(state, "Tools", "Brightness")
    turn(state, 6)
    press(B)
    run(state)
    assert prefs.brightness() == 12


def test_the_knob_cannot_exceed_the_power_ceiling(state):
    import prefs

    go(state, "Tools", "Brightness")
    turn(state, 400)
    assert state._editor.value == prefs.MAX_BRIGHTNESS
    assert setup.neopixels.brightness <= prefs.MAX_BRIGHTNESS / 100.0


def test_the_knob_cannot_turn_the_panel_off(state):
    import prefs

    go(state, "Tools", "Brightness")
    turn(state, -400)
    assert state._editor.value == prefs.MIN_BRIGHTNESS


def test_the_saved_brightness_is_applied_while_the_badge_warms(state):
    """The panel comes up at the built-in default; warming corrects it."""
    import prefs

    prefs.set_brightness(22)
    setup.neopixels.brightness = 0.1
    state._warmed = 0
    while state.warm_step():
        pass
    assert setup.neopixels.brightness == pytest.approx(0.22)


def test_the_screen_text_lives_under_tools(state):
    go(state, "Tools")
    assert "Screen text" in [item.label for item in state.menu.items]


def test_setting_the_screen_text_asks_for_one(state):
    go(state, "Tools", "Screen text")
    assert state._entry is not None


def test_the_screen_text_is_written_to_the_card(state):
    import prefs

    go(state, "Tools", "Screen text")
    name_it(state, "HELLO")
    assert prefs.text() == "HELLO"


def test_renaming_the_screen_text_starts_from_what_is_there(state):
    import prefs

    prefs.set_text("OLD")
    go(state, "Tools", "Screen text")
    assert state._entry.text == "OLD", "it started from empty"
