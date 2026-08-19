"""Tests for the sequencer singleton: the layer binding engine to hardware.

These need the CircuitPython stubs, unlike the engine tests. The stub mixer
enforces its voice count and the stub NeoPixel its length, so routing mistakes
raise here the same way they would on the badge.
"""

import struct

import pytest

import circuitpython_stubs
import sequencer as sequencer_module
from engine.song import DEFAULT_VELOCITY, MAX_VELOCITY, TRACK_COUNT
from engine.transport import LIVE, SEQ
from sequencer import AUDITION_VOICE, MIXER_VOICES, VOICES_PER_TRACK, Sequencer


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
def seq():
    return Sequencer()


@pytest.fixture
def kit(tmp_path):
    return [write_wav(tmp_path / ("t%d.wav" % t)) for t in range(TRACK_COUNT)]


# --- audio path -----------------------------------------------------------


def test_audio_is_allocated_once_at_import():
    """The singleton owns the I2S output for the life of the program."""
    assert sequencer_module.engine.audio is not None
    assert sequencer_module.engine.mixer is not None


def test_mixer_has_room_for_polyphonic_mode(seq):
    """Built with headroom so the mode toggle never rebuilds it mid-pattern."""
    assert len(seq.mixer.voice) == MIXER_VOICES
    assert MIXER_VOICES >= TRACK_COUNT * VOICES_PER_TRACK + 2


def test_mixer_matches_the_sample_format(seq):
    assert seq.mixer.config["sample_rate"] == sequencer_module.SAMPLE_RATE
    assert seq.mixer.config["channel_count"] == 1
    assert seq.mixer.config["bits_per_sample"] == 16


# --- kit loading ----------------------------------------------------------


def test_loading_a_kit(seq, kit):
    assert seq.load_kit(kit) == TRACK_COUNT
    assert all(seq.has_sample(t) for t in range(TRACK_COUNT))


def test_a_missing_sample_leaves_the_track_silent(seq):
    assert seq.load_track(0, "/nope/missing.wav") is False
    assert not seq.has_sample(0)


def test_a_malformed_sample_leaves_the_track_silent(seq, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    assert seq.load_track(0, str(bad)) is False
    assert not seq.has_sample(0)


def test_a_bad_sample_does_not_disturb_other_tracks(seq, kit):
    seq.load_kit(kit)
    seq.load_track(3, "/nope/missing.wav")
    assert not seq.has_sample(3)
    assert seq.has_sample(2) and seq.has_sample(4)


def test_a_ram_loaded_track_holds_no_file_handle(seq, kit):
    """Loading into RAM closes the file at once - nothing to leak."""
    seq.load_track(0, kit[0])
    assert seq.is_streamed(0) is False
    assert seq._files[0] is None


def test_triggering_a_silent_track_is_harmless(seq):
    assert seq.trigger(0, 100) is False


# --- voice routing --------------------------------------------------------


def test_one_voice_per_track_is_the_default(seq):
    """The locked decision for this rework was one voice per track.

    Two alternating voices let a hit ring through its own retrigger, which is
    worth having - but it plays one sample object on two mixer voices at
    once. That is harmless for a sample in RAM and corrupting for a streamed
    one, which would share a file position and a read buffer between them.
    It belongs behind a setting, which is where the decision put it.
    """
    assert seq.poly is False


def test_the_first_hit_uses_the_tracks_first_voice(seq, kit):
    seq.load_kit(kit)
    seq.poly = True
    assert seq._voice_for(0) == 0
    assert seq._voice_for(1) == VOICES_PER_TRACK


def test_mono_mode_reuses_one_voice_per_track(seq, kit):
    """A retrigger cuts its own previous hit, as a 909 does."""
    seq.load_kit(kit)
    seq.poly = False
    assert seq._voice_for(0) == seq._voice_for(0) == 0
    assert seq._voice_for(1) == VOICES_PER_TRACK


def test_poly_mode_alternates_between_two_voices(seq, kit):
    """The old hit decays instead of being cut, which removes the click."""
    seq.load_kit(kit)
    seq.poly = True
    first = seq._voice_for(0)
    second = seq._voice_for(0)
    assert first != second
    assert {first, second} == {0, 1}
    assert seq._voice_for(0) == first, "should alternate back"


def test_every_track_routes_inside_the_mixer(seq):
    for poly in (False, True):
        seq.poly = poly
        for track in range(TRACK_COUNT):
            for _ in range(3):
                assert 0 <= seq._voice_for(track) < MIXER_VOICES


def test_tracks_never_share_a_voice(seq):
    seq.poly = True
    seen = {}
    for track in range(TRACK_COUNT):
        for _ in range(2):
            voice = seq._voice_for(track)
            assert seen.get(voice, track) == track, "voice %d shared" % voice
            seen[voice] = track


def test_audition_does_not_use_a_track_voice(seq, kit):
    assert AUDITION_VOICE >= TRACK_COUNT * VOICES_PER_TRACK
    seq.audition(kit[0])
    assert seq.mixer.voice[AUDITION_VOICE].playing


def test_velocity_scales_the_voice_level(seq, kit):
    """Velocity sets the level, scaled by the master volume.

    Full velocity is not full scale: the mixer sums its voices, so a single
    hit has to leave room for the others playing with it.
    """
    seq.load_kit(kit)
    seq.trigger(0, MAX_VELOCITY)
    assert seq.mixer.voice[0].level == pytest.approx(seq.volume)
    seq.trigger(0, 64)
    assert seq.mixer.voice[0].level == pytest.approx(seq.volume * 64 / 127.0, abs=0.01)


# --- transport wiring -----------------------------------------------------


def test_play_starts_the_clock(seq):
    seq.toggle_play()
    assert seq.transport.playing
    assert seq.clock.running


def test_stop_stops_the_clock(seq):
    seq.toggle_play()
    seq.toggle_play()
    assert not seq.transport.playing
    assert not seq.clock.running


def test_starting_resets_the_playhead(seq):
    seq.clock.tick = 40
    seq.toggle_play()
    assert seq.clock.tick == 0


def test_a_pad_hit_punches_in_when_armed(seq, kit):
    seq.load_kit(kit)
    seq.toggle_record()
    assert seq.pad_hit(0) is True
    assert seq.transport.playing
    assert seq.clock.running


def test_a_punched_in_hit_is_recorded(seq, kit):
    seq.load_kit(kit)
    seq.toggle_record()
    seq.pad_hit(3)
    assert seq.song.is_on(3, 0)


def track_is_sounding(seq, track):
    """True if any of the track's voices is playing.

    Which one is deliberately not asserted: with two voices per track the
    engine alternates, so pinning a specific index would break every time the
    voice strategy changed without anything actually being wrong.
    """
    base = track * VOICES_PER_TRACK
    return any(
        seq.mixer.voice[base + offset].playing for offset in range(VOICES_PER_TRACK)
    )


def test_a_pad_hit_sounds_even_when_not_recording(seq, kit):
    seq.load_kit(kit)
    seq.pad_hit(0)
    assert track_is_sounding(seq, 0)


def test_pads_do_not_record_in_seq_mode(seq, kit):
    seq.load_kit(kit)
    seq.toggle_play()
    seq.toggle_record()
    seq.mode = SEQ
    seq.pad_hit(2)
    assert seq.song.is_empty()


# --- capture and erase ----------------------------------------------------


def test_capture_writes_at_the_current_position(seq):
    seq.clock.tick = 4 * seq.song.ticks_per_step
    assert seq.capture(1) == 4
    assert seq.song.velocity(1, 4) == DEFAULT_VELOCITY


def test_erase_clears_at_the_current_position(seq):
    seq.song.set_step(1, 4, 100)
    seq.clock.tick = 4 * seq.song.ticks_per_step
    seq.erase(1)
    assert not seq.song.is_on(1, 4)


def test_erase_leaves_other_steps_alone(seq):
    seq.song.set_step(1, 4, 100)
    seq.song.set_step(1, 5, 100)
    seq.clock.tick = 4 * seq.song.ticks_per_step
    seq.erase(1)
    assert seq.song.is_on(1, 5)


# --- playback -------------------------------------------------------------


def test_a_step_sounds_when_its_tick_arrives(seq, kit):
    seq.load_kit(kit)
    seq.song.set_step(0, 0, 100)
    seq.toggle_play()
    seq._on_tick(0)
    assert track_is_sounding(seq, 0)


def test_a_muted_track_stays_silent(seq, kit):
    seq.load_kit(kit)
    seq.song.set_step(0, 0, 100)
    seq.song.toggle_mute(0)
    seq.toggle_play()
    seq._on_tick(0)
    assert not track_is_sounding(seq, 0)


def test_current_step_follows_the_clock(seq):
    seq.song.set_length(16)
    seq.clock.tick = 6 * 5
    assert seq.current_step == 5


# --- MIDI out -------------------------------------------------------------


def test_midi_out_is_off_by_default(seq, kit):
    """The engine keeps running in MIDI mode, so it must not blurt by default."""
    seq.load_kit(kit)
    seq.trigger(0, 100)
    assert seq.midi_out == [False] * TRACK_COUNT


def test_enabling_midi_out_sends_a_note(seq, kit):
    from setup import midi_serial

    seq.load_kit(kit)
    midi_serial.sent.clear()
    seq.midi_out[0] = True
    seq.trigger(0, 100)
    assert len(midi_serial.sent) == 1


# --- settings -------------------------------------------------------------


def test_strength_is_clamped(seq):
    assert seq.set_strength(-1) == 0.0
    assert seq.set_strength(9) == 1.0


def test_strength_nudges_by_a_step(seq):
    seq.set_strength(0.5)
    seq.nudge_strength(-1)
    assert seq.strength < 0.5


def test_mode_toggles(seq):
    assert seq.mode == LIVE
    assert seq.toggle_mode() == SEQ
    assert seq.toggle_mode() == LIVE


def test_track_selection_wraps(seq):
    assert seq.select_track(TRACK_COUNT) == 0
    assert seq.select_track(3) == 3


def test_page_wraps_within_the_pattern(seq):
    seq.song.set_length(16)  # two pages
    assert seq.set_page(0) == 0
    assert seq.set_page(1) == 1
    assert seq.set_page(2) == 0


# --- sync and MIDI transport ----------------------------------------------


def test_sync_does_not_start_a_stopped_transport_by_default(seq):
    """A stray clock on a busy patch should not decide the badge is playing."""
    assert seq.sync_starts_transport is False


def test_sync_can_be_allowed_to_start_the_transport(seq):
    import setup

    seq.sync_starts_transport = True
    setup.sync_in.value = True
    seq._poll_sync_in(0)
    setup.sync_in.value = False
    seq._poll_sync_in(10)
    assert seq.transport.playing
    setup.sync_in.value = True


def test_sync_sets_tempo_without_starting_when_not_allowed(seq):
    import setup

    seq.sync_starts_transport = False
    setup.sync_in.value = True
    seq._poll_sync_in(0)
    setup.sync_in.value = False
    seq._poll_sync_in(10)
    assert not seq.transport.playing
    assert seq.clock.source == "ext", "still latched for tempo and phase"
    setup.sync_in.value = True


def test_midi_start_starts_the_transport(seq):
    from setup import midi_serial

    midi_serial.post(circuitpython_stubs.Start())
    seq.poll_midi_in()
    assert seq.transport.playing
    assert seq.clock.tick == 0


def test_midi_stop_stops_the_transport(seq):
    from setup import midi_serial

    midi_serial.post(circuitpython_stubs.Start())
    seq.poll_midi_in()
    midi_serial.post(circuitpython_stubs.Stop())
    seq.poll_midi_in()
    assert not seq.transport.playing


def test_midi_continue_does_not_reset_the_playhead(seq):
    from setup import midi_serial

    seq.clock.tick = 40
    midi_serial.post(circuitpython_stubs.Continue())
    seq.poll_midi_in()
    assert seq.transport.playing
    assert seq.clock.tick == 40


def test_per_track_strength_reaches_the_engine(seq):
    seq.set_strength(1.0)
    seq.set_track_strength(2, 0.0)
    assert seq.strength_for(2) == 0.0
    assert seq.strength_for(3) == 1.0


# --- sample locations -----------------------------------------------------


def fake_lister(tree):
    def lister(directory):
        if directory not in tree:
            raise OSError("no such directory")
        return tree[directory]

    return lister


def test_samples_are_found_across_both_stores():
    lister = fake_lister({"/sd/samples": ["a.wav"], "/samples": ["b.wav"]})
    found = sequencer_module.list_samples(lister)
    assert [n for n, _ in found] == ["a.wav", "b.wav"]


def test_the_sd_card_shadows_flash_for_the_same_name():
    lister = fake_lister({"/sd/samples": ["kick.wav"], "/samples": ["kick.wav"]})
    found = sequencer_module.list_samples(lister)
    assert found == [("kick.wav", "/sd/samples/kick.wav")]


def test_a_missing_directory_is_not_an_error():
    """A badge with no card must still list its onboard samples."""
    lister = fake_lister({"/samples": ["kick.wav"]})
    assert sequencer_module.list_samples(lister) == [("kick.wav", "/samples/kick.wav")]


def test_non_wav_files_are_ignored():
    lister = fake_lister({"/samples": ["kick.wav", "readme.txt", ".hidden.wav"]})
    assert [n for n, _ in sequencer_module.list_samples(lister)] == ["kick.wav"]


def test_a_bare_name_resolves_to_a_full_path():
    lister = fake_lister({"/sd/samples": ["kick.wav"]})
    assert sequencer_module.resolve_sample("kick.wav", lister) == "/sd/samples/kick.wav"


def test_an_absolute_path_is_left_alone():
    lister = fake_lister({})
    assert sequencer_module.resolve_sample("/x/y.wav", lister) == "/x/y.wav"


def test_an_unknown_name_resolves_to_nothing():
    lister = fake_lister({"/samples": []})
    assert sequencer_module.resolve_sample("nope.wav", lister) is None


# --- RAM loading versus streaming -----------------------------------------


def write_wav_sized(path, data_bytes, rate=None, channels=1, bits=16):
    if rate is None:
        rate = sequencer_module.SAMPLE_RATE
    frames = data_bytes // 2
    data = struct.pack("<%dh" % frames, *([0] * frames))
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH",
            16,
            1,
            channels,
            rate,
            rate * channels * bits // 8,
            channels * bits // 8,
            bits,
        )
        + b"data"
        + struct.pack("<I", len(data))
    )
    path.write_bytes(header + data)
    return str(path)


def test_a_short_sample_is_loaded_into_ram(seq, tmp_path):
    """Storage cannot feed the mixer reliably; RAM takes it out of the path."""
    path = write_wav_sized(tmp_path / "short.wav", 8 * 1024)
    assert seq.load_track(0, path) is True
    assert seq.has_sample(0)
    assert seq.is_streamed(0) is False
    assert seq.ram_used == 8 * 1024


def test_an_oversized_sample_falls_back_to_streaming(seq, tmp_path):
    """One long sound in a kit should still play, just from storage."""
    path = write_wav_sized(tmp_path / "long.wav", sequencer_module.MAX_RAM_SAMPLE * 2)
    assert seq.load_track(0, path) is True
    assert seq.has_sample(0)
    assert seq.is_streamed(0) is True
    assert seq.ram_used == 0


def test_the_ram_budget_is_not_exceeded(seq, tmp_path):
    size = sequencer_module.MAX_RAM_SAMPLE
    for track in range(TRACK_COUNT):
        path = write_wav_sized(tmp_path / ("t%d.wav" % track), size)
        seq.load_track(track, path)
    assert seq.ram_used <= sequencer_module.RAM_BUDGET


def test_tracks_past_the_budget_stream_instead_of_failing(seq, tmp_path):
    size = sequencer_module.MAX_RAM_SAMPLE
    for track in range(TRACK_COUNT):
        path = write_wav_sized(tmp_path / ("t%d.wav" % track), size)
        assert seq.load_track(track, path) is True, "every track must still load"
    assert any(seq.is_streamed(t) for t in range(TRACK_COUNT))


def test_reloading_a_track_returns_its_ram(seq, tmp_path):
    path = write_wav_sized(tmp_path / "a.wav", 8 * 1024)
    seq.load_track(0, path)
    assert seq.ram_used == 8 * 1024
    seq.load_track(0, path)
    assert seq.ram_used == 8 * 1024, "budget must not leak on reload"


def test_a_wrong_rate_sample_is_refused_with_a_reason(seq, tmp_path):
    """The mixer has one fixed format; 44.1k would play an octave down."""
    path = write_wav_sized(tmp_path / "44k.wav", 4096, rate=44100)
    assert seq.load_track(0, path) is False
    assert not seq.has_sample(0)
    assert "44100" in seq.last_error
    assert str(sequencer_module.SAMPLE_RATE) in seq.last_error


def test_a_stereo_sample_is_refused(seq, tmp_path):
    path = write_wav_sized(tmp_path / "st.wav", 4096, channels=2)
    assert seq.load_track(0, path) is False
    assert "2ch" in seq.last_error


def test_reloading_a_streamed_track_closes_its_file(seq, tmp_path):
    """Only streamed tracks hold a handle, and it must not leak on reload."""
    big = write_wav_sized(tmp_path / "big.wav", sequencer_module.MAX_RAM_SAMPLE * 2)
    seq.load_track(0, big)
    assert seq.is_streamed(0) is True
    first = seq._files[0]
    assert first is not None

    seq.load_track(0, big)
    assert first.closed, "the previous handle must be closed"


def test_releasing_a_streamed_track_closes_its_file(seq, tmp_path):
    big = write_wav_sized(tmp_path / "big2.wav", sequencer_module.MAX_RAM_SAMPLE * 2)
    seq.load_track(3, big)
    handle = seq._files[3]
    seq.load_track(3, None)
    assert handle.closed
    assert not seq.has_sample(3)


def test_a_ram_loaded_sample_can_actually_be_played(seq, tmp_path):
    """RawSample infers bit depth from the buffer's element size.

    Handing it raw bytes makes it 8-bit, which constructs happily and only
    fails at play() with "bits_per_sample does not match". This exercises the
    play path so that mistake cannot reach hardware again.
    """
    path = write_wav_sized(tmp_path / "ram.wav", 8 * 1024)
    seq.load_track(0, path)
    assert seq.is_streamed(0) is False
    assert seq.trigger(0, 100) is True
    assert track_is_sounding(seq, 0)


def test_a_ram_loaded_sample_is_sixteen_bit(seq, tmp_path):
    path = write_wav_sized(tmp_path / "ram2.wav", 4096)
    seq.load_track(0, path)
    assert seq._samples[0].bits_per_sample == 16


def test_midi_is_polled_on_a_timer_not_every_pass(seq, monkeypatch):
    """Reading the ports costs about 430us; the loop is otherwise ~200us."""
    calls = []
    monkeypatch.setattr(seq, "poll_midi_in", lambda now=None: calls.append(1))

    times = iter([0, 1, 1, 2, 3, 4, 5])
    monkeypatch.setattr(sequencer_module, "ticks_ms", lambda: next(times))
    for _ in range(6):
        seq.tick()
    assert 0 < len(calls) < 6, "polled sometimes, not every pass"


def test_sync_input_is_still_polled_every_pass(seq, monkeypatch):
    """An edge lasts milliseconds; missing one loses the beat."""
    polls = []
    monkeypatch.setattr(seq, "_poll_sync_in", lambda now: polls.append(now))
    times = iter([0, 1, 2, 3, 4, 5])
    monkeypatch.setattr(sequencer_module, "ticks_ms", lambda: next(times))
    for _ in range(6):
        seq.tick()
    assert len(polls) == 6


def test_streamed_tracks_use_the_largest_allowed_buffer(seq, tmp_path):
    """CircuitPython caps WaveFile's buffer at 1024 bytes.

    That cap is what limits streaming: the card sustains 679 KB/s in 4 KB
    reads but only 333 KB/s in 1 KB ones. Asking for more raises ValueError,
    so this pins the value at the maximum the runtime permits.
    """
    assert sequencer_module.STREAM_BUFFER == 1024
    path = write_wav_sized(tmp_path / "big3.wav", sequencer_module.MAX_RAM_SAMPLE * 2)
    assert seq.load_track(0, path) is True
    assert seq.is_streamed(0) is True
    assert len(seq._samples[0].buffer) == sequencer_module.STREAM_BUFFER


# --- falling through to a usable copy -------------------------------------


def test_candidates_include_every_store_holding_the_name():
    lister = fake_lister({"/sd/samples": ["kick.wav"], "/samples": ["kick.wav"]})
    assert sequencer_module.sample_candidates("kick.wav", lister) == [
        "/sd/samples/kick.wav",
        "/samples/kick.wav",
    ]


def test_candidates_keep_the_card_first():
    lister = fake_lister({"/sd/samples": ["a.wav"], "/samples": ["a.wav"]})
    assert sequencer_module.sample_candidates("a.wav", lister)[0].startswith("/sd")


def test_an_absolute_path_has_exactly_one_candidate():
    assert sequencer_module.sample_candidates("/x/y.wav", fake_lister({})) == [
        "/x/y.wav"
    ]


def test_an_unusable_copy_falls_through_to_a_playable_one(seq, tmp_path, monkeypatch):
    """A stale copy on the card must not make a track silent.

    This is the real situation it was found in: samples left on an SD card at
    a previous mixer rate shadowed correct copies in flash by name, and every
    track reported as failing to load.
    """
    card = tmp_path / "card"
    flash = tmp_path / "flash"
    card.mkdir()
    flash.mkdir()
    write_wav_sized(card / "kick.wav", 4096, rate=22050)  # wrong rate, shadows
    write_wav_sized(flash / "kick.wav", 4096)  # correct rate

    monkeypatch.setattr(
        sequencer_module, "SAMPLE_DIRS", (str(card), str(flash)), raising=False
    )
    monkeypatch.setattr(
        sequencer_module,
        "sample_candidates",
        lambda name, lister=None, dirs=None: [
            str(card / "kick.wav"),
            str(flash / "kick.wav"),
        ],
    )

    assert seq.load_track(0, "kick.wav") is True
    assert seq.has_sample(0)
    assert seq.song.kit[0] == str(flash / "kick.wav"), "should use the playable copy"


def test_a_track_still_fails_when_no_copy_is_usable(seq, tmp_path, monkeypatch):
    bad = write_wav_sized(tmp_path / "bad.wav", 4096, rate=44100)
    monkeypatch.setattr(
        sequencer_module,
        "sample_candidates",
        lambda name, lister=None, dirs=None: [bad],
    )
    assert seq.load_track(0, "bad.wav") is False
    assert not seq.has_sample(0)


# --- the stream only runs when there is something to play -----------------


def test_no_stream_is_running_at_rest(seq):
    """An idle stream makes every screen redraw pop the amplifier.

    Confirmed on hardware: identical display traffic is silent with the
    stream stopped and pops with it running. The original firmware only
    created its I2S output on entering the sampler, so menus were quiet.
    """
    assert seq.streaming is False


def test_triggering_starts_the_stream(seq, kit):
    seq.load_kit(kit)
    seq.trigger(0, 100)
    assert seq.streaming is True


def test_starting_the_transport_starts_the_stream(seq):
    seq.toggle_play()
    assert seq.streaming is True


def test_auditioning_starts_the_stream(seq, kit):
    seq.audition(kit[0])
    assert seq.streaming is True


def test_the_stream_keeps_running_while_the_transport_plays(seq, kit):
    seq.load_kit(kit)
    seq.toggle_play()
    seq._update_stream(sequencer_module.STREAM_LINGER_MS * 10)
    assert seq.streaming is True


def test_the_stream_keeps_running_while_a_voice_sounds(seq, kit):
    seq.load_kit(kit)
    seq.trigger(0, 100)
    seq._update_stream(sequencer_module.STREAM_LINGER_MS * 10)
    assert seq.streaming is True, "a decaying hit must not be cut off"


def test_the_stream_stops_once_everything_is_quiet(seq, kit):
    seq.load_kit(kit)
    seq.trigger(0, 100)
    for voice in seq.mixer.voice:
        voice.playing = False
    seq._update_stream(seq._last_sound + sequencer_module.STREAM_LINGER_MS + 1)
    assert seq.streaming is False


def test_the_stream_lingers_briefly_rather_than_flapping(seq, kit):
    """Stopping between hits would stop and start it constantly."""
    seq.load_kit(kit)
    seq.trigger(0, 100)
    for voice in seq.mixer.voice:
        voice.playing = False
    seq._update_stream(seq._last_sound + sequencer_module.STREAM_LINGER_MS // 2)
    assert seq.streaming is True


def test_a_hit_after_the_stream_stopped_starts_it_again(seq, kit):
    seq.load_kit(kit)
    seq.stop_stream()
    assert seq.streaming is False
    seq.trigger(0, 100)
    assert seq.streaming is True


# --- out of the box -------------------------------------------------------


def test_a_fresh_engine_comes_up_playable(seq, kit):
    """A badge out of the box must not give eight silent pads and no pattern.

    This repeats what the module does at import rather than inspecting the
    live singleton, whose song and kit other tests legitimately change. The
    kit here is temporary files: whether /samples/Kick.wav exists is a
    property of the badge, not of this code.
    """
    fresh = Sequencer()
    assert fresh.load_kit(kit) > 0, "kit loading is broken"
    fresh.load_demo_pattern()
    assert any(fresh.has_sample(t) for t in range(TRACK_COUNT))
    assert not fresh.song.is_empty(), "Play would do nothing"


def test_the_default_kit_is_named_by_bare_filenames(seq):
    """So it resolves wherever the samples live, card or flash."""
    assert sequencer_module.DEFAULT_KIT
    assert not any(name.startswith("/") for name in sequencer_module.DEFAULT_KIT)


def test_the_demo_pattern_is_a_recognisable_beat(seq):
    seq.load_demo_pattern()
    song = seq.song
    assert song.length == 16
    assert [s for s in range(16) if song.is_on(0, s)] == [0, 4, 8, 12]
    assert [s for s in range(16) if song.is_on(1, s)] == [4, 12]
    assert [s for s in range(16) if song.is_on(2, s)] == [2, 6, 10, 14]


def test_the_demo_pattern_replaces_whatever_was_there(seq):
    seq.song.set_step(7, 3, 100)
    seq.load_demo_pattern()
    assert not seq.song.is_on(7, 3)


def test_loading_the_demo_pattern_does_not_start_the_stream(seq):
    """Only playing should start it; building a pattern is silent."""
    seq.stop_stream()
    seq.load_demo_pattern()
    assert seq.streaming is False


# --- a sample the badge cannot play ----------------------------------------
#
# The kit loads at import, so any exception that escapes a load handler fails
# the badge at boot rather than skipping one bad file. CircuitPython's own WAV
# parser runs inside WaveFile() and can raise things engine.wav never sees,
# so the handlers have to cover more than engine.wav's own errors.


def _forcing_streaming(monkeypatch):
    """Push every sample down the streaming branch instead of the RAM one."""
    monkeypatch.setattr(sequencer_module, "MAX_RAM_SAMPLE", 0)


def test_a_sample_that_exhausts_ram_while_streaming_is_skipped(seq, kit, monkeypatch):
    """MemoryError is neither OSError nor ValueError, so it needs naming."""
    _forcing_streaming(monkeypatch)

    def out_of_memory(file_obj, buffer=None):
        raise MemoryError("no room")

    monkeypatch.setattr(sequencer_module, "WaveFile", out_of_memory)
    assert seq.load_kit(kit) == 0
    assert not seq.has_sample(0)


def test_a_bad_sample_does_not_take_the_whole_kit_down(seq, kit, monkeypatch):
    """One unreadable file must cost one track, not the badge."""
    _forcing_streaming(monkeypatch)
    real = sequencer_module.WaveFile
    calls = []

    def sometimes(file_obj, buffer=None):
        calls.append(1)
        if len(calls) == 1:
            raise MemoryError("no room")
        return real(file_obj, buffer)

    monkeypatch.setattr(sequencer_module, "WaveFile", sometimes)
    assert seq.load_kit(kit) == TRACK_COUNT - 1
    assert not seq.has_sample(0)
    assert seq.has_sample(1)


def test_a_failed_stream_does_not_leak_the_file_handle(seq, kit, monkeypatch):
    """Handles are the scarce resource; a load that fails must return one."""
    _forcing_streaming(monkeypatch)

    def out_of_memory(file_obj, buffer=None):
        raise MemoryError("no room")

    monkeypatch.setattr(sequencer_module, "WaveFile", out_of_memory)
    seq.load_kit(kit)
    assert all(handle is None for handle in seq._files)


# --- the audio path failing underneath us ---------------------------------
#
# Observed on the badge: voice.play raised OSError(EIO) from inside the main
# loop and ended the program mid-pattern. The badge is an instrument; one
# hit that will not sound is recoverable, the instrument stopping is not.


def _exploding_voice(seq, exc):
    voice = seq.mixer.voice[0]

    def boom(sample, **kwargs):
        raise exc

    voice.play = boom
    return voice


def test_a_failing_voice_does_not_stop_the_sequencer(seq, kit):
    seq.load_kit(kit)
    _exploding_voice(seq, OSError(5, "Input/output error"))
    assert seq.trigger(0, 100) is False
    assert seq.audio_errors == 1


def test_a_failing_voice_is_recorded_for_diagnosis(seq, kit):
    """A silent skip turns a hardware fault into mysteriously missing hits."""
    seq.load_kit(kit)
    _exploding_voice(seq, OSError(5, "Input/output error"))
    seq.trigger(0, 100)
    assert isinstance(seq.last_audio_error, OSError)


def test_other_tracks_still_sound_after_one_fails(seq, kit):
    seq.load_kit(kit)
    _exploding_voice(seq, OSError(5, "boom"))
    seq.trigger(0, 100)
    assert seq.trigger(1, 100) is True


def test_a_tick_survives_a_failing_voice(seq, kit):
    """The failure arrived through tick(), so that is what must not raise."""
    seq.load_kit(kit)
    seq.song.set_step(0, 0, 100)
    _exploding_voice(seq, OSError(5, "boom"))
    seq.toggle_play()
    for _ in range(50):
        seq.tick()


def test_a_working_voice_is_not_reported_as_an_error(seq, kit):
    seq.load_kit(kit)
    assert seq.trigger(0, 100) is True
    assert seq.audio_errors == 0
    assert seq.last_audio_error is None


def test_a_failing_voice_silences_the_output(seq, kit):
    """Containing the error is not enough on its own.

    Observed on the badge: the hit was skipped and the sequencer carried on,
    but the I2S peripheral was left looping its buffer, which is a loud
    continuous noise rather than one missing drum. Silence is the only
    acceptable result of an audio path that has just failed.
    """
    seq.load_kit(kit)
    seq.trigger(1, 100)
    assert seq._streaming is True
    _exploding_voice(seq, OSError(5, "boom"))
    seq.trigger(0, 100)
    assert seq._streaming is False, "the output was left running"


def test_a_teardown_that_also_fails_does_not_escape(seq, kit):
    """The path is already faulty; the recovery must not raise on top of it."""
    seq.load_kit(kit)
    _exploding_voice(seq, OSError(5, "boom"))

    def also_boom():
        raise OSError(5, "stop failed too")

    seq.stop_stream = also_boom
    assert seq.trigger(0, 100) is False


def test_the_next_hit_can_still_start_the_stream(seq, kit):
    """Silencing must not wedge the sampler into permanent silence."""
    seq.load_kit(kit)
    good = seq.mixer.voice[0].play
    _exploding_voice(seq, OSError(5, "boom"))
    seq.trigger(0, 100)
    seq.mixer.voice[0].play = good
    assert seq.trigger(0, 100) is True
    assert seq._streaming is True


# --- the memory the audio actually lives in -------------------------------
#
# A RAM sample is a RawSample wrapping a memoryview of bytes read off the
# card. The I2S DMA reads that memory for as long as the sample can play, but
# the reference the sample holds is not one the garbage collector traces. If
# the bytes are only a local, they become collectable the moment loading
# returns, and playing the sample later reads memory that has since been
# reused. That is a hard fault rather than an exception: the badge drops
# straight to safe mode with no traceback, which is exactly what it did.


def test_a_ram_sample_keeps_its_audio_alive(seq, kit):
    seq.load_kit(kit)
    for track in range(TRACK_COUNT):
        assert not seq.is_streamed(track), "this test is about the RAM path"
        assert seq._audio[track] is not None, "track %d holds no buffer" % track


def test_the_buffer_is_the_audio_that_was_read(seq, kit):
    """Holding the wrong object would satisfy a reference count and nothing else.

    What is held is the 16-bit view the sample was handed, so its length is
    in samples; the byte count is what the loader accounted for.
    """
    seq.load_kit(kit)
    assert seq._audio[0].nbytes == seq._sizes[0]


def test_releasing_a_track_lets_its_audio_go(seq, kit):
    """Held too long is a leak; on this board that is 24 KB a track."""
    seq.load_kit(kit)
    seq._release_track(0)
    assert seq._audio[0] is None


def test_reloading_a_track_replaces_its_buffer(seq, kit):
    seq.load_kit(kit)
    first = seq._audio[0]
    seq.load_track(0, kit[1])
    assert seq._audio[0] is not None
    assert seq._audio[0] is not first


def test_a_streamed_track_holds_no_buffer(seq, kit, monkeypatch):
    """Only the RAM path has audio to keep; streaming reads as it goes."""
    monkeypatch.setattr(sequencer_module, "MAX_RAM_SAMPLE", 0)
    seq.load_kit(kit)
    assert seq.is_streamed(0)
    assert seq._audio[0] is None


def test_a_streamed_track_keeps_its_read_buffer(seq, kit, monkeypatch):
    """Same defect as the RAM path, one function away.

    WaveFile reads through the bytearray it was handed. Passing a fresh one
    as an argument and keeping no other name for it makes it collectable as
    soon as loading returns, and the audio then reads whatever replaced it.
    """
    monkeypatch.setattr(sequencer_module, "MAX_RAM_SAMPLE", 0)
    seq.load_kit(kit)
    assert seq.is_streamed(0)
    assert seq._stream_buffers[0] is not None
    assert len(seq._stream_buffers[0]) == sequencer_module.STREAM_BUFFER


def test_releasing_a_streamed_track_lets_its_buffer_go(seq, kit, monkeypatch):
    monkeypatch.setattr(sequencer_module, "MAX_RAM_SAMPLE", 0)
    seq.load_kit(kit)
    seq._release_track(0)
    assert seq._stream_buffers[0] is None


def test_an_auditioned_sample_outlives_the_call(seq, kit):
    """Nothing else owns it: the voice is playing a local otherwise."""
    assert seq.audition(kit[0]) is True
    assert seq._audition_sample is not None
    assert seq._audition_buffer is not None


def test_a_failed_audition_holds_nothing(seq, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav file at all")
    assert seq.audition(str(bad)) is False
    assert seq._audition_sample is None


def test_every_playing_sample_is_owned_somewhere(seq, kit):
    """The rule this class of bug keeps breaking, stated once.

    Anything CircuitPython plays holds a pointer, not a traceable reference.
    If the sequencer is not holding it, nothing is.
    """
    seq.load_kit(kit)
    for track in range(TRACK_COUNT):
        assert seq._samples[track] is not None
        held = seq._audio[track] is not None or seq._stream_buffers[track] is not None
        assert held, "track %d plays through a buffer nothing owns" % track


# --- how loud the mixer is asked to be ------------------------------------
#
# audiomixer sums its voices. Levels are a polyphony budget, not a taste
# setting: whatever the sum reaches above full scale is clipped, which is a
# crunch rather than a quiet distortion. The shipped beat puts a kick and a
# snare on the same step, so this is reached immediately.


def worst_simultaneous_level(seq):
    """The loudest sum the current pattern can ask the mixer for."""
    song = seq.song
    worst = 0.0
    for step in range(song.length):
        total = 0.0
        for track in range(TRACK_COUNT):
            velocity = song.velocity(track, step)
            if velocity:
                total += seq.volume * (velocity / 127.0)
        if total > worst:
            worst = total
    return worst


def test_the_shipped_beat_does_not_clip(seq):
    """Two voices on one step summed to 1.65 of full scale before this."""
    seq.load_demo_pattern()
    assert worst_simultaneous_level(seq) <= 1.0


def test_a_full_chord_of_four_reaches_full_scale_but_no_further(seq):
    """The budget: four voices at full velocity is the design limit."""
    song = seq.song
    song.clear_all()
    for track in range(4):
        song.set_step(track, 0, MAX_VELOCITY)
    assert worst_simultaneous_level(seq) <= 1.0


# --- what the MIDI poll costs when nothing is connected -------------------
#
# receive() allocates whether or not a message arrives - measured on the
# badge at 32 bytes a call on the serial port and 64 on USB. On a 2 ms timer
# that is about 15 KB of garbage a second, from the same heap the audio path
# needs, with nothing plugged into either port. Free memory was seen dipping
# to a couple of hundred bytes while a pattern played.


def test_the_serial_port_is_not_read_when_nothing_is_waiting(seq):
    """The UART can be asked, for free. Ask before allocating."""
    calls = []
    real = sequencer_module.midi_serial.receive
    sequencer_module.midi_uart.in_waiting = 0

    def counted():
        calls.append(1)
        return real()

    sequencer_module.midi_serial.receive = counted
    try:
        for _ in range(50):
            seq.poll_midi_in(now=0)
    finally:
        sequencer_module.midi_serial.receive = real
    assert calls == []


def test_the_serial_port_is_read_when_bytes_are_waiting(seq):
    sequencer_module.midi_serial.post(circuitpython_stubs.Start())
    seq.poll_midi_in(now=0)
    assert seq.transport.playing


def test_usb_is_polled_on_its_own_slower_timer(seq):
    """USB cannot be asked whether anything is waiting, so poll it less."""
    calls = []
    real = sequencer_module.midi_usb.receive

    def counted():
        calls.append(1)
        return real()

    sequencer_module.midi_usb.receive = counted
    try:
        seq.poll_midi_in(now=0)
        first = len(calls)
        for step in range(1, sequencer_module.USB_MIDI_POLL_MS):
            seq.poll_midi_in(now=step)
        assert len(calls) == first, "USB polled faster than its own timer"
        seq.poll_midi_in(now=sequencer_module.USB_MIDI_POLL_MS)
        assert len(calls) == first + 1
    finally:
        sequencer_module.midi_usb.receive = real


def test_usb_transport_messages_still_arrive(seq):
    sequencer_module.midi_usb.post(circuitpython_stubs.Start())
    seq.poll_midi_in(now=sequencer_module.USB_MIDI_POLL_MS * 2)
    assert seq.transport.playing


def test_the_usb_timer_survives_the_tick_rollover(seq):
    """ticks_ms wraps at 2**29; a plain subtraction stops polling at the wrap."""
    # Five milliseconds before the wrap, asked again well after it: the gap
    # is 35 ms, comfortably past the interval, but a plain subtraction reads
    # it as hugely negative and never fires again.
    seq._last_usb_midi_poll = (1 << 29) - 5
    calls = []
    real = sequencer_module.midi_usb.receive
    sequencer_module.midi_usb.receive = lambda: calls.append(1)
    try:
        seq.poll_midi_in(now=30)
    finally:
        sequencer_module.midi_usb.receive = real
    assert calls, "USB stopped being polled across the wrap"
