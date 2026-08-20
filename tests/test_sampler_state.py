"""Tests for the sampler UI state.

Drives the state the way the main loop does - post key events, call update -
and checks the effect on the engine. The stubs make this possible off-hardware.
"""

import struct

import pytest

import circuitpython_stubs
from conftest import FakeMachine
import sequencer as sequencer_module
import setup
from engine import view
from engine.controls import FUNCTION, PLAY, SELECT, VOLUME
from engine.song import STEPS_PER_PAGE
from engine.transport import LIVE, SEQ
import SamplerState as sampler_module
from SamplerState import SamplerState


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


def run(state, machine=None, passes=8):
    """Pump the state the way the main loop does.

    A single pass handles a bounded number of key events, because unbounded
    draining is the same failure mode the redraw budget exists to prevent.
    The real loop turns over thousands of times a second, so a backlog is
    cleared microseconds later; a test that calls update once is modelling a
    loop that runs once.
    """
    machine = machine or FakeMachine()
    for _ in range(passes):
        state.update(machine)


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


def test_select_click_returns_to_settings(state):
    machine = FakeMachine()
    press(SELECT)
    state.update(machine)
    assert machine.transitions == ["settings"]


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


def test_the_display_is_not_redrawn_on_every_pass(state):
    """Redrawing text is what pops the amplifier, so it has to be rationed.

    Counting real calls, not recomputing the throttle: a test that re-derives
    `passes % REDRAW_EVERY` would still pass if the redraw were deleted.
    """
    calls = []
    real = state._render_display

    def counted():
        calls.append(1)
        return real()

    state._render_display = counted
    passes = sampler_module.REDRAW_EVERY * 3
    for _ in range(passes):
        state.update(None)
    assert 0 < len(calls) <= 4, len(calls)
    assert len(calls) < passes / 10, "redrawing far too often"


def test_an_encoder_edit_reaches_the_pixels(state):
    """The gate is only safe if every path that changes a colour marks it.

    A velocity nudge changes a pad's brightness without moving the playhead
    or the blink phase, so without an explicit mark it would not be drawn
    until some unrelated event happened to invalidate the cache.
    """
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.song.set_step(0, 0, 40)
    for action, value in state.controls.press(0):
        state._act(action, value, None)
    # Render after the press, so the press's own invalidation is spent and
    # only the encoder turn can account for a change.
    state._render(force=True)
    before = list(state._last_pixels)

    setup.select_enc.position += 5
    state._handle_encoders()
    state._render()

    assert engine.song.velocity(0, 0) > 40, "the nudge itself must have landed"
    assert state._last_pixels != before, "brighter step, unchanged pixels"


def test_changing_the_pattern_length_reaches_the_pixels(state):
    """Moving the loop point changes which pads read as out of pattern."""
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.song.set_length(16)
    for action, value in state.controls.press(FUNCTION):
        state._act(action, value, None)
    state._render(force=True)
    before = list(state._last_pixels)

    setup.select_enc.position -= 12
    state._handle_encoders()
    state._render()

    assert engine.song.length < 16, "the length change must have landed"
    assert state._last_pixels != before, "loop point moved, unchanged pixels"


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
        seen.add(tuple(state._screen.line(i) for i in range(len(state._screen))))
    assert len(seen) == 1, "the screen must not change as the playhead moves"


def test_the_display_still_updates_when_the_player_changes_something(state):
    engine = sequencer_module.engine
    state._render_display()
    before = [state._screen.line(i) for i in range(len(state._screen))]
    engine.set_bpm(engine.clock.bpm + 10)
    state._render_display()
    after = [state._screen.line(i) for i in range(len(state._screen))]
    assert after != before


def test_only_the_line_that_changed_is_rewritten(state):
    """A change must not resend lines that did not change; that is the fix."""
    engine = sequencer_module.engine
    state._render_display()
    top_before = state._screen.line(0)
    engine.set_bpm(engine.clock.bpm + 10)  # only the detail line carries BPM
    state._render_display()
    assert state._screen.line(0) == top_before
    assert "%d" % engine.clock.bpm in state._screen.line(1)


def test_pads_still_show_the_playhead(state):
    """It has to be visible somewhere, and the LEDs are the quiet place."""
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.toggle_play()
    engine.clock.tick = 0
    state._render(force=True)
    first = list(state._last_pixels)
    engine.clock.tick = 3 * engine.song.ticks_per_step
    state._render()
    assert state._last_pixels != first


def test_a_moved_playhead_still_reaches_the_pixels(state):
    """The gate must not swallow the one update that changes every step."""
    engine = sequencer_module.engine
    engine.mode = SEQ
    state.controls.set_mode(SEQ)
    engine.toggle_play()
    for step in range(STEPS_PER_PAGE):
        engine.clock.tick = step * engine.song.ticks_per_step
        state._render()
        assert state._last_pixels[step] == view.PLAYHEAD


def test_pixels_are_not_rebuilt_when_nothing_moved(state):
    """The main loop turns over about 4000 times a second; rebuilding the
    colour list on every pass is continuous allocation for no visible change.
    """
    state._render(force=True)
    calls = []
    real = state._render_pixels

    def counted(playhead, blink):
        calls.append(1)
        return real(playhead, blink)

    state._render_pixels = counted
    for _ in range(50):
        state._render()
    assert calls == [], "nothing moved, so nothing should have been rebuilt"


def test_a_pad_hit_still_lights_the_pad(state):
    """Flashes are the case the gate is most likely to miss: they change the
    colours without moving the playhead or the blink phase.
    """
    engine = sequencer_module.engine
    engine.mode = LIVE
    state.controls.set_mode(LIVE)
    state._render(force=True)
    quiet = list(state._last_pixels)
    for action, value in state.controls.press(0):
        state._act(action, value, None)
    state._render()
    assert state._last_pixels != quiet


def test_a_bounded_pass_does_not_drop_the_events_it_did_not_handle(state):
    """Bounding the drain must not eat the events it refuses to process.

    Fetching an event and then discovering the budget is spent loses it: it
    has already left the queue. A release lost that way leaves a modifier
    stuck down for the rest of the session, which is worse than the
    unbounded drain the bound was added to prevent.
    """
    engine = sequencer_module.engine
    engine.mode = LIVE
    state.controls.set_mode(LIVE)

    for key in (FUNCTION, PLAY):
        press(key)
    for key in (PLAY, FUNCTION):
        release(key)
    assert 4 > module_max_events(), "the test needs more events than one pass takes"

    run(state)
    assert not state.controls._function_held, "FUNCTION never came back up"
    assert not state.controls._play_held, "PLAY never came back up"


def module_max_events():
    import SamplerState as module

    return module.MAX_EVENTS_PER_PASS


# --- the volume knob ------------------------------------------------------
#
# A safety control: it is what someone wearing headphones reaches for when
# a sound is too loud. It has to be on the bare knob, with no modifier to
# remember, and the level has to be visible before it is audible.


def test_the_volume_knob_changes_the_volume(state):
    engine = sequencer_module.engine
    before = engine.volume
    setup.volume_enc.position += 3
    state._handle_encoders()
    assert engine.volume > before


def test_turning_the_volume_knob_down_makes_it_quieter(state):
    engine = sequencer_module.engine
    engine.set_volume(0.5)
    setup.volume_enc.position -= 2
    state._handle_encoders()
    assert engine.volume < 0.5


def test_the_bare_knob_is_volume(state):
    """No modifier to hold. Reaching for a chord to turn something down is
    the wrong shape for the one control with a safety job.
    """
    assert state.controls.volume_turn_target() == "volume"


def test_holding_function_still_gets_division(state):
    for action, value in state.controls.press(FUNCTION):
        state._act(action, value, None)
    assert state.controls.volume_turn_target() == "division"


def test_the_volume_is_on_the_display(state):
    engine = sequencer_module.engine
    engine.set_volume_position(24)
    state._render_display()
    assert "V50" in state._screen.line(1), state._screen.line(1)


def test_the_displayed_volume_follows_the_knob(state):
    engine = sequencer_module.engine
    engine.set_volume_position(24)
    state._render_display()
    before = state._screen.line(1)
    setup.volume_enc.position += 2
    state._handle_encoders()
    state._render_display()
    assert state._screen.line(1) != before


def test_spinning_the_volume_knob_hard_moves_it_far(state):
    """One pass of the loop can see many detents at once."""
    engine = sequencer_module.engine
    engine.set_volume(1.0)
    setup.volume_enc.position -= 10
    state._handle_encoders()
    assert engine.volume < 0.6


# --- how quickly the numbers on screen follow a knob ----------------------
#
# The text was only rebuilt every REDRAW_EVERY passes, so tempo and volume
# lagged the knob by a noticeable fraction of a second - long enough that a
# value could not be dialled in by watching it.


def test_the_display_follows_the_volume_knob_at_once(state):
    engine = sequencer_module.engine
    engine.set_volume_position(20)
    state._render(force=True)
    before = state._screen.line(1)

    setup.volume_enc.position += 4
    state._handle_encoders()
    state._render()  # the very next pass, not four hundred later

    assert state._screen.line(1) != before


def test_the_display_follows_the_tempo_knob_at_once(state):
    engine = sequencer_module.engine
    engine.set_bpm(120)
    state._render(force=True)
    before = state._screen.line(1)

    setup.select_enc.position += 5
    state._handle_encoders()
    state._render()

    assert state._screen.line(1) != before
    assert str(int(engine.clock.bpm)) in state._screen.line(1)


def test_an_idle_pass_still_does_not_rebuild_the_text(state):
    """Immediacy must not cost a rebuild on every one of thousands of passes.

    Driven through update, because that is what advances the pass counter
    the periodic rebuild is keyed to.
    """
    machine = FakeMachine()
    state.update(machine)
    calls = []
    real = state._render_display

    def counted():
        calls.append(1)
        return real()

    state._render_display = counted
    for _ in range(module_redraw_every() - 2):
        state.update(machine)
    assert calls == [], "rebuilt %d times while nothing changed" % len(calls)


def module_redraw_every():
    import SamplerState as module

    return module.REDRAW_EVERY
