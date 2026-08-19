"""Tests for the sampler UI state.

Drives the state the way the main loop does - post key events, call update -
and checks the effect on the engine. The stubs make this possible off-hardware.
"""

import struct

import pytest

import circuitpython_stubs
import sequencer as sequencer_module
import setup
from engine.controls import FUNCTION, PLAY, SELECT, VOLUME
from engine.transport import LIVE, SEQ
from SamplerState import SamplerState


class FakeMachine:
    def __init__(self):
        self.animation = None
        self.last_state = None
        self.transitions = []

    def go_to_state(self, name):
        self.transitions.append(name)


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
def state(tmp_path):
    engine = sequencer_module.engine
    engine.transport.stop()
    engine.clock.stop()
    engine.clock.reset()
    engine.song.clear_all()
    engine.song.set_length(16)
    engine.song.set_division(3)
    engine.clock.set_bpm(120)
    engine.set_strength(1.0)
    engine.mode = LIVE
    engine.selected_track = 0
    engine.set_page(0)
    for track in range(8):
        engine.load_track(track, write_wav(tmp_path / ("t%d.wav" % track)))
    setup.keys.events.clear()
    setup.select_enc.position = 0
    setup.volume_enc.position = 0
    s = SamplerState()
    s.enter(FakeMachine())
    return s


def press(key):
    setup.keys.events.post(circuitpython_stubs.Event(key, pressed=True))


def release(key):
    setup.keys.events.post(circuitpython_stubs.Event(key, pressed=False))


def run(state, machine=None):
    state.update(machine or FakeMachine())


# --- live pads ------------------------------------------------------------


def test_a_pad_triggers_its_track(state):
    press(2)
    run(state)
    engine = sequencer_module.engine
    base = 2 * sequencer_module.VOICES_PER_TRACK
    assert any(
        engine.mixer.voice[base + i].playing
        for i in range(sequencer_module.VOICES_PER_TRACK)
    )


def test_a_pad_does_not_record_when_not_armed(state):
    press(2)
    run(state)
    assert sequencer_module.engine.song.is_empty()


def test_a_struck_pad_flashes_then_stops(state):
    press(2)
    run(state)
    assert 2 in state._flash
    for _ in range(60):
        run(state)
    assert 2 not in state._flash


# --- mode and navigation --------------------------------------------------


def test_function_tapped_switches_mode(state):
    press(FUNCTION)
    release(FUNCTION)
    run(state)
    assert state.controls.mode == SEQ
    assert sequencer_module.engine.mode == SEQ


def test_function_plus_pad_selects_a_track(state):
    press(FUNCTION)
    press(4)
    release(4)
    release(FUNCTION)
    run(state)
    assert sequencer_module.engine.selected_track == 4
    assert state.controls.mode == LIVE, "selecting must not also switch mode"


def test_select_click_returns_to_the_menu(state):
    machine = FakeMachine()
    press(SELECT)
    state.update(machine)
    assert machine.transitions == ["menu"]


def test_leaving_does_not_stop_the_beat(state):
    """The engine is a singleton; the menu is just a different screen."""
    engine = sequencer_module.engine
    engine.toggle_play()
    machine = FakeMachine()
    press(SELECT)
    state.update(machine)
    state.exit(machine)
    assert engine.transport.playing
    assert engine.clock.running


# --- step editing ---------------------------------------------------------


def tap(state, pad):
    """A full press and release, which is what decides a step in SEQ."""
    press(pad)
    run(state)
    release(pad)
    run(state)


def test_a_step_is_not_toggled_until_the_pad_is_released(state):
    """The press might be the start of a velocity edit, so nothing happens yet."""
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    press(3)
    run(state)
    assert not engine.song.is_on(engine.selected_track, 3)


def test_a_pad_toggles_a_step_in_seq_mode(state):
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    tap(state, 3)
    assert engine.song.is_on(engine.selected_track, 3)


def test_tapping_a_lit_step_clears_it(state):
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    tap(state, 3)
    assert engine.song.is_on(engine.selected_track, 3), "first tap must set it"
    tap(state, 3)
    assert not engine.song.is_on(engine.selected_track, 3)


def test_step_editing_respects_the_page(state):
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.set_page(1)
    tap(state, 0)
    assert engine.song.is_on(engine.selected_track, 8)


def test_a_step_past_the_loop_point_cannot_be_set(state):
    engine = sequencer_module.engine
    engine.song.set_length(4)
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    tap(state, 6)
    assert not engine.song.is_on(engine.selected_track, 6)


def test_play_plus_pad_pages_in_seq(state):
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    press(PLAY)
    press(1)
    run(state)
    assert engine.page == 1


# --- transport ------------------------------------------------------------


def test_play_starts_and_stops(state):
    engine = sequencer_module.engine
    press(PLAY)
    release(PLAY)
    run(state)
    assert engine.transport.playing
    press(PLAY)
    release(PLAY)
    run(state)
    assert not engine.transport.playing


def test_function_plus_play_arms_record(state):
    engine = sequencer_module.engine
    press(FUNCTION)
    press(PLAY)
    run(state)
    assert engine.transport.armed or engine.transport.recording


def test_arming_does_not_also_toggle_mode_or_transport(state):
    engine = sequencer_module.engine
    press(FUNCTION)
    press(PLAY)
    release(PLAY)
    release(FUNCTION)
    run(state)
    assert state.controls.mode == LIVE
    assert not engine.transport.playing


def test_a_pad_hit_while_armed_punches_in_and_records(state):
    engine = sequencer_module.engine
    press(FUNCTION)
    press(PLAY)
    release(PLAY)
    release(FUNCTION)
    run(state)
    press(1)
    run(state)
    assert engine.transport.playing
    assert engine.song.is_on(1, 0)


# --- encoders -------------------------------------------------------------


def test_the_select_encoder_changes_tempo(state):
    engine = sequencer_module.engine
    before = engine.clock.bpm
    setup.select_enc.position += 5
    run(state)
    assert engine.clock.bpm == before + 5


def test_function_puts_length_on_the_select_encoder(state):
    engine = sequencer_module.engine
    before = engine.song.length
    press(FUNCTION)
    run(state)
    setup.select_enc.position += 3
    run(state)
    assert engine.song.length == before + 3
    assert engine.clock.bpm == 120, "tempo must not move too"


def test_function_puts_division_on_the_volume_encoder(state):
    engine = sequencer_module.engine
    before = engine.song.division
    press(FUNCTION)
    run(state)
    setup.volume_enc.position += 1
    run(state)
    assert engine.song.division == before + 1


def test_play_puts_quantise_on_the_volume_encoder(state):
    engine = sequencer_module.engine
    engine.set_strength(1.0)
    press(PLAY)
    run(state)
    setup.volume_enc.position -= 1
    run(state)
    assert engine.strength < 1.0


def test_holding_a_pad_edits_that_steps_velocity(state):
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.song.set_step(engine.selected_track, 2, 100)
    press(2)
    run(state)
    setup.select_enc.position += 2
    run(state)
    assert engine.song.velocity(engine.selected_track, 2) > 100


def test_editing_a_velocity_does_not_also_toggle_the_step_off(state):
    """One gesture means two things; the edit must win over the toggle."""
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.song.set_step(engine.selected_track, 2, 100)
    press(2)
    run(state)
    setup.select_enc.position += 2
    run(state)
    release(2)
    run(state)
    assert engine.song.is_on(engine.selected_track, 2), "still on after an edit"
    assert engine.song.velocity(engine.selected_track, 2) > 100


def test_shrinking_the_pattern_pulls_the_page_back(state):
    """Otherwise the view sits on a page that no longer exists."""
    engine = sequencer_module.engine
    engine.song.set_length(64)
    engine.set_page(7)
    press(FUNCTION)
    run(state)
    setup.select_enc.position -= 60
    run(state)
    assert engine.page < engine.song.page_count


# --- muting and clearing --------------------------------------------------


def test_volume_click_mutes_the_selected_track(state):
    engine = sequencer_module.engine
    press(VOLUME)
    run(state)
    assert engine.song.muted[engine.selected_track]


def test_function_plus_volume_click_clears_the_track(state):
    engine = sequencer_module.engine
    engine.song.set_step(0, 1, 100)
    engine.song.set_step(0, 5, 100)
    press(FUNCTION)
    press(VOLUME)
    run(state)
    assert not engine.song.is_on(0, 1)
    assert not engine.song.is_on(0, 5)


# --- rendering ------------------------------------------------------------


def test_pixels_are_only_pushed_when_they_change(state):
    before = setup.neopixels.show_count
    for _ in range(20):
        run(state)
    assert setup.neopixels.show_count == before, "nothing changed, nothing to push"


def test_pixels_are_pushed_when_something_changes(state):
    before = setup.neopixels.show_count
    press(2)
    run(state)
    assert setup.neopixels.show_count > before


def test_the_display_is_not_redrawn_every_pass(state):
    """A frame costs ~13ms; the loop runs in ~250us."""
    setup.display.shown = None
    drawn = 0
    for _ in range(50):
        run(state)
        if setup.display.shown is not None:
            drawn += 1
            setup.display.shown = None
    assert drawn < 50


def test_the_display_does_not_follow_the_playhead(state):
    """A frame costs ~32ms of I2C and that traffic pops the amplifier.

    Putting the playhead on screen would change the text every step, so a
    frame would be sent every step. The pad LEDs carry the playhead instead.
    """
    engine = sequencer_module.engine
    engine.song.set_step(engine.selected_track, 0, 100)
    engine.song.set_step(engine.selected_track, 4, 100)
    engine.toggle_play()

    seen = set()
    for step in range(engine.song.length):
        engine.clock.tick = step * engine.song.ticks_per_step
        state._render_display()
        seen.add(state._shown)
    assert len(seen) == 1, "the screen must not change as the playhead moves"


def test_the_display_still_updates_when_the_player_changes_something(state):
    engine = sequencer_module.engine
    state._render_display()
    before = state._shown
    engine.set_bpm(engine.clock.bpm + 10)
    state._shown = None
    state._render_display()
    assert state._shown != before


def test_pads_still_show_the_playhead(state):
    """It has to be visible somewhere, and the LEDs are the quiet place."""
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.toggle_play()
    engine.clock.tick = 0
    state._render_pixels(engine.current_step)
    first = list(state._last_pixels)
    engine.clock.tick = 3 * engine.song.ticks_per_step
    state._render_pixels(engine.current_step)
    assert state._last_pixels != first
