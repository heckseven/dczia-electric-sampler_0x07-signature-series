"""Tests for the sequencer singleton: the layer binding engine to hardware.

These need the CircuitPython stubs, unlike the engine tests. The stub mixer
enforces its voice count and the stub NeoPixel its length, so routing mistakes
raise here the same way they would on the badge.
"""

import struct

import pytest

import circuitpython_stubs
import sequencer as sequencer_module
from engine.song import DEFAULT_VELOCITY, TRACK_COUNT
from engine.transport import LIVE, SEQ
from sequencer import AUDITION_VOICE, MIXER_VOICES, VOICES_PER_TRACK, Sequencer


def write_wav(path, frames=64):
    data = struct.pack("<%dh" % frames, *([0] * frames))
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 22050, 44100, 2, 16)
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
    assert seq.mixer.config["sample_rate"] == 22050
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
    seq.load_kit(kit)
    seq.trigger(0, 127)
    assert seq.mixer.voice[0].level == pytest.approx(1.0)
    seq.trigger(0, 64)
    assert seq.mixer.voice[0].level == pytest.approx(64 / 127.0, abs=0.01)


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


def test_a_pad_hit_sounds_even_when_not_recording(seq, kit):
    seq.load_kit(kit)
    seq.pad_hit(0)
    assert seq.mixer.voice[0].playing


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
    assert seq.mixer.voice[0].playing


def test_a_muted_track_stays_silent(seq, kit):
    seq.load_kit(kit)
    seq.song.set_step(0, 0, 100)
    seq.song.toggle_mute(0)
    seq.toggle_play()
    seq._on_tick(0)
    assert not seq.mixer.voice[0].playing


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


def write_wav_sized(path, data_bytes, rate=22050, channels=1, bits=16):
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
    assert seq.mixer.voice[0].playing


def test_a_ram_loaded_sample_is_sixteen_bit(seq, tmp_path):
    path = write_wav_sized(tmp_path / "ram2.wav", 4096)
    seq.load_track(0, path)
    assert seq._samples[0].bits_per_sample == 16


def test_midi_is_polled_on_a_timer_not_every_pass(seq, monkeypatch):
    """Reading the ports costs about 430us; the loop is otherwise ~200us."""
    calls = []
    monkeypatch.setattr(seq, "poll_midi_in", lambda: calls.append(1))

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
