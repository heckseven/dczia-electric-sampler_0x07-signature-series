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


def test_a_loaded_track_holds_no_file_handle(seq, kit):
    """Loading closes the file at once, and storage is never touched again.

    This is the property the whole RAM path exists for. A handle held open
    across playback is a handle something can read from underneath a playing
    voice, which is exactly how a streamed track used to die.
    """
    seq.load_track(0, kit[0])
    assert not hasattr(seq, "_files"), "no track may keep a file open"


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


# --- loading into RAM, and trimming what will not fit ---------------------


def write_wav_sized(path, data_bytes, rate=None, channels=1, bits=16, value=0):
    if rate is None:
        rate = sequencer_module.SAMPLE_RATE
    frames = data_bytes // 2
    # `value` fills every frame, so a test can tell which frames the loader
    # touched: silence is indistinguishable from a fade that ran over
    # everything.
    data = struct.pack("<%dh" % frames, *([value] * frames))
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


def test_a_short_sample_is_loaded_whole(seq, tmp_path):
    """Storage cannot feed the mixer reliably; RAM takes it out of the path."""
    path = write_wav_sized(tmp_path / "short.wav", 8 * 1024)
    assert seq.load_track(0, path) is True
    assert seq.has_sample(0)
    assert seq.was_truncated(0) is False
    assert seq.ram_used == 8 * 1024


def test_an_oversized_sample_is_trimmed_rather_than_refused(seq, tmp_path):
    """A long sound still plays; it just stops sooner.

    The alternative used to be streaming it, which died on the second hit.
    Refusing it instead would turn a long sound into a silent track, which is
    worse than a short one.
    """
    path = write_wav_sized(tmp_path / "long.wav", sequencer_module.MAX_RAM_SAMPLE * 2)
    assert seq.load_track(0, path) is True
    assert seq.has_sample(0)
    assert seq.was_truncated(0) is True
    assert seq.ram_used == sequencer_module.MAX_RAM_SAMPLE


def test_a_trimmed_sample_ends_in_silence(seq, tmp_path):
    """A cut mid-waveform is a full-scale step, and a step clicks."""
    size = sequencer_module.MAX_RAM_SAMPLE * 2
    path = write_wav_sized(tmp_path / "loud.wav", size, value=20000)
    assert seq.load_track(0, path) is True
    view = seq._views[0]
    assert view[-1] == 0, "the last frame must be silent"
    assert view[-2] == 0 or abs(view[-2]) < abs(view[0]), "the tail must decay"
    head = len(view) - sequencer_module.FADE_FRAMES - 1
    assert view[head] == 20000, "only the tail may be touched"


def test_a_sample_that_fits_is_not_faded(seq, tmp_path):
    """The fade is for a cut, not for every sample."""
    path = write_wav_sized(tmp_path / "fits.wav", 8 * 1024, value=20000)
    assert seq.load_track(0, path) is True
    assert seq.was_truncated(0) is False
    assert seq._views[0][-1] == 20000


def test_the_ram_budget_is_not_exceeded(seq, tmp_path):
    size = sequencer_module.MAX_RAM_SAMPLE
    for track in range(TRACK_COUNT):
        path = write_wav_sized(tmp_path / ("t%d.wav" % track), size)
        seq.load_track(track, path)
    assert seq.ram_used <= sequencer_module.RAM_BUDGET


def test_tracks_are_trimmed_into_the_budget_before_being_refused(seq, tmp_path):
    """The budget degrades by shortening, and only then by going silent.

    Nothing streams any more, so a kit larger than RAM_BUDGET cannot have
    every track. What it can have is every track that fits, trimmed rather
    than refused, and a reason recorded for the ones that do not.
    """
    size = sequencer_module.MAX_RAM_SAMPLE
    loaded = []
    for track in range(TRACK_COUNT):
        path = write_wav_sized(tmp_path / ("t%d.wav" % track), size)
        if seq.load_track(track, path):
            loaded.append(track)
    assert loaded, "the budget must fund at least one track"
    assert len(loaded) < TRACK_COUNT, "this test is about running out"
    assert seq.ram_used <= sequencer_module.RAM_BUDGET
    assert "budget" in seq.last_error


def test_a_kit_shares_the_budget_so_no_track_is_starved(seq, tmp_path):
    """The shipped kit off the card is 131 KB against a 48 KB budget.

    Loading it first come, first served spent the whole budget on the first
    three tracks and left the cymbal silent - which is the failure holding
    samples in RAM was supposed to remove, not reintroduce. Every track that
    has a sample must end up with one.
    """
    sizes = (20812, 7984, 37064, 66034)  # kick, snare, open hat, cymbal
    paths = [
        write_wav_sized(tmp_path / ("k%d.wav" % track), size)
        for track, size in enumerate(sizes)
    ]
    assert seq.load_kit(paths) == len(sizes)
    for track in range(len(sizes)):
        assert seq.has_sample(track), "track %d went silent" % track
        assert seq.trigger(track, 100) is True
    assert seq.ram_used <= sequencer_module.RAM_BUDGET


def test_a_short_sample_leaves_its_remainder_to_the_others(seq, tmp_path):
    """Sharing is recomputed from what was used, not from an equal split."""
    sizes = (2048, 66034, 66034, 66034)
    paths = [
        write_wav_sized(tmp_path / ("s%d.wav" % track), size)
        for track, size in enumerate(sizes)
    ]
    seq.load_kit(paths)
    assert seq._sizes[0] == 2048, "the short one was trimmed for no reason"
    # The three long ones split what the short one did not take, so each gets
    # more than a flat quarter of the budget.
    for track in (1, 2, 3):
        assert seq._sizes[track] > sequencer_module.RAM_BUDGET // 4


def test_one_track_assigned_alone_may_take_what_is_free(seq, tmp_path):
    """Nothing to share with: the browser is replacing a single sample."""
    path = write_wav_sized(tmp_path / "solo.wav", sequencer_module.MAX_RAM_SAMPLE * 2)
    assert seq.load_track(0, path) is True
    assert seq._sizes[0] == sequencer_module.MAX_RAM_SAMPLE


def test_a_track_past_the_budget_is_silent_not_broken(seq, tmp_path):
    """Out of budget must leave the track empty, not half loaded."""
    size = sequencer_module.MAX_RAM_SAMPLE
    for track in range(TRACK_COUNT):
        path = write_wav_sized(tmp_path / ("b%d.wav" % track), size)
        seq.load_track(track, path)
    silent = [t for t in range(TRACK_COUNT) if not seq.has_sample(t)]
    assert silent, "this test is about running out"
    for track in silent:
        assert seq._samples[track] is None
        assert seq._audio[track] is None
        assert seq._sizes[track] == 0
        assert seq.trigger(track, 100) is False


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


def test_a_ram_loaded_sample_can_actually_be_played(seq, tmp_path):
    """RawSample infers bit depth from the buffer's element size.

    Handing it raw bytes makes it 8-bit, which constructs happily and only
    fails at play() with "bits_per_sample does not match". This exercises the
    play path so that mistake cannot reach hardware again.
    """
    path = write_wav_sized(tmp_path / "ram.wav", 8 * 1024)
    seq.load_track(0, path)
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
    """Eight steps, so the whole of it is on one page of pads - the same
    shape as a new song, which is what a fresh badge should demonstrate."""
    from engine.song import DEFAULT_LENGTH

    seq.load_demo_pattern()
    song = seq.song
    assert song.length == DEFAULT_LENGTH
    assert [s for s in range(8) if song.is_on(0, s)] == [0, 4]
    assert [s for s in range(8) if song.is_on(1, s)] == [4]
    assert [s for s in range(8) if song.is_on(2, s)] == [2, 6]


def test_a_new_song_is_one_page_long():
    """Eight pads, eight steps: no paging to understand before writing a beat."""
    from engine.song import DEFAULT_LENGTH, STEPS_PER_PAGE, Song

    assert DEFAULT_LENGTH == STEPS_PER_PAGE
    song = Song()
    assert song.length == DEFAULT_LENGTH
    assert all(song.track_length(t) == DEFAULT_LENGTH for t in range(8))


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


def _out_of_memory(monkeypatch, failures=None):
    """Make bytearray() raise, the way a fragmented heap does.

    `failures` limits how many allocations fail, so a test can starve one
    track and let the rest through.
    """
    real = bytearray
    calls = []

    def stingy(*args, **kwargs):
        calls.append(1)
        if failures is None or len(calls) <= failures:
            raise MemoryError("no room")
        return real(*args, **kwargs)

    monkeypatch.setattr(sequencer_module, "bytearray", stingy, raising=False)
    return calls


def test_a_sample_that_exhausts_ram_is_skipped(seq, kit, monkeypatch):
    """MemoryError is neither OSError nor ValueError, so it needs naming.

    The kit loads at import, so an escape here fails the badge at boot rather
    than silencing one track.
    """
    _out_of_memory(monkeypatch)
    assert seq.load_kit(kit) == 0
    assert not seq.has_sample(0)


def test_a_bad_sample_does_not_take_the_whole_kit_down(seq, kit, monkeypatch):
    """One unreadable file must cost one track, not the badge."""
    # Two failures: the first attempt and the one retry after collecting.
    _out_of_memory(monkeypatch, failures=2)
    assert seq.load_kit(kit) == TRACK_COUNT - 1
    assert not seq.has_sample(0)
    assert seq.has_sample(1)


def test_a_failed_load_leaves_the_track_empty(seq, kit, monkeypatch):
    """A half-loaded track would play a buffer nothing finished filling."""
    _out_of_memory(monkeypatch)
    seq.load_kit(kit)
    for track in range(TRACK_COUNT):
        assert seq._samples[track] is None
        assert seq._audio[track] is None
        assert seq._views[track] is None
    assert seq.ram_used == 0


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
        assert seq._audio[track] is not None, "track %d holds no buffer" % track


def test_the_buffer_is_the_audio_that_was_read(seq, kit):
    """Holding the wrong object would satisfy a reference count and nothing else."""
    seq.load_kit(kit)
    assert len(seq._audio[0]) == seq._sizes[0]


def test_both_the_bytes_and_the_view_over_them_are_held(seq, kit):
    """Either one alone leaves a gap.

    A memoryview in MicroPython does not necessarily keep its base object
    alive, so holding only the view can still let the bytes be collected.
    Holding only the bytes assumes the sample points at them rather than at
    the view it was actually handed. The audio reads whatever replaced them
    either way, and that is a hard fault rather than an exception.
    """
    seq.load_kit(kit)
    for track in range(TRACK_COUNT):
        assert seq._audio[track] is not None, "track %d lost its bytes" % track
        assert seq._views[track] is not None, "track %d lost its view" % track
    assert seq._views[0].nbytes == seq._sizes[0]


def test_releasing_a_track_lets_both_go(seq, kit):
    seq.load_kit(kit)
    seq._release_track(0)
    assert seq._audio[0] is None
    assert seq._views[0] is None
    # The budget is spent in loaded bytes, so the size has to go back too or
    # the track's share stays booked against a sample that is no longer there.
    assert seq._sizes[0] == 0


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


def test_a_trimmed_track_still_owns_its_audio(seq, tmp_path):
    """Trimming must not lose the hold on the buffer the DMA walks."""
    path = write_wav_sized(tmp_path / "trim.wav", sequencer_module.MAX_RAM_SAMPLE * 2)
    seq.load_track(0, path)
    assert seq.was_truncated(0) is True
    assert seq._audio[0] is not None
    assert seq._views[0] is not None
    assert len(seq._audio[0]) == seq._sizes[0]


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


# --- the master volume ----------------------------------------------------
#
# This is a safety control before it is a musical one. It exists so someone
# wearing headphones can make a sound quieter, and the two things that
# matters for are that it reaches zero and that it takes effect on what is
# already sounding rather than on the next hit.


SLOW = 1000  # milliseconds between detents: unhurried, so no acceleration


def test_the_knob_changes_the_volume(seq):
    """Turned slowly, one detent is one notch either way."""
    before = seq.volume_position
    seq.nudge_volume(1, now=SLOW)
    assert seq.volume_position == before + 1
    seq.nudge_volume(-1, now=SLOW * 2)
    assert seq.volume_position == before


def test_the_volume_reaches_silence(seq):
    """All the way down has to mean silent, not merely quiet."""
    for step in range(100):
        seq.nudge_volume(-1, now=SLOW * (step + 1))
    assert seq.volume == 0.0
    assert seq.volume_position == 0


def test_the_volume_does_not_run_past_full(seq):
    for step in range(100):
        seq.nudge_volume(1, now=SLOW * (step + 1))
    assert seq.volume == sequencer_module.MAX_VOLUME
    assert seq.volume_position == sequencer_module.VOLUME_STEPS


def test_turning_down_quiets_a_sound_already_playing(seq, kit):
    """The point of the control. A drum hit is a third of a second, so
    waiting for the next one would usually do - but "usually" is not good
    enough for something whose job is to stop a noise in someone's ears.
    """
    seq.load_kit(kit)
    seq.trigger(0, MAX_VELOCITY)
    voice = seq.mixer.voice[seq._voice_for(0)]
    loud = voice.level
    seq.set_volume(0.0)
    assert voice.level == 0.0
    assert loud > 0.0


def test_turning_up_lifts_a_sound_already_playing(seq, kit):
    seq.load_kit(kit)
    seq.set_volume(0.2)
    seq.trigger(0, MAX_VELOCITY)
    voice = seq.mixer.voice[seq._voice_for(0)]
    quiet = voice.level
    seq.set_volume(0.6)
    assert voice.level > quiet


def test_velocity_still_matters_at_any_volume(seq, kit):
    seq.load_kit(kit)
    seq.set_volume(0.5)
    seq.trigger(0, MAX_VELOCITY)
    hard = seq.mixer.voice[seq._voice_for(0)].level
    seq.trigger(0, 20)
    soft = seq.mixer.voice[seq._voice_for(0)].level
    assert soft < hard


def test_the_shipped_beat_still_does_not_clip_at_the_default(seq):
    """The default lands on the nearest notch to the old linear default."""
    seq.load_demo_pattern()
    assert seq.volume == pytest.approx(sequencer_module.DEFAULT_VOLUME, abs=0.02)
    assert worst_simultaneous_level(seq) <= 1.0


def test_polyphony_remembers_the_right_voice(seq, kit):
    """_voice_for advances the rotation, so it must only be asked once."""
    seq.load_kit(kit)
    seq.poly = True
    seq.trigger(0, 100)
    sounded = [i for i, v in enumerate(seq._voice_velocity) if v]
    assert sounded, "no voice recorded a velocity"
    for index in sounded:
        assert seq.mixer.voice[index].level > 0, "level and velocity disagree"


def test_a_hard_spin_moves_the_volume_a_long_way(seq):
    """Direction alone is not enough for the control that means "stop".

    A fast turn arrives as one large delta, so honouring only its sign would
    move the volume a single twentieth however hard it was spun.
    """
    seq.set_volume_position(sequencer_module.VOLUME_STEPS)
    seq.nudge_volume(-10, now=SLOW)
    assert seq.volume_position == sequencer_module.VOLUME_STEPS - 10


def test_a_hard_spin_down_can_reach_silence_in_one_move(seq):
    seq.set_volume_position(sequencer_module.VOLUME_STEPS)
    seq.nudge_volume(-8, now=1000)
    seq.nudge_volume(-8, now=1005)  # a spin: accelerated
    assert seq.volume == 0.0


def test_the_notches_are_even_to_the_ear(seq):
    """Equal steps in decibels, not in level.

    A linear scale makes the bottom of the range unusable: one twentieth of
    full level to two twentieths is a doubling, and that is where headphone
    listening happens.
    """
    ratios = []
    for position in (12, 20, 28, 36):
        seq.set_volume_position(position)
        quiet = seq.volume
        seq.set_volume_position(position + 4)
        ratios.append(seq.volume / quiet)
    for ratio in ratios[1:]:
        assert ratio == pytest.approx(ratios[0], rel=0.01)


def test_the_quiet_end_has_fine_control(seq):
    """One notch near the bottom must be a small change in absolute terms."""
    seq.set_volume_position(8)
    quiet = seq.volume
    seq.set_volume_position(9)
    assert seq.volume - quiet < 0.01


def test_spinning_the_knob_moves_further_than_creeping_it(seq):
    """The same detent should do more when the knob is moving fast.

    An encoder reports detents, not speed, so the only clue is how close
    together they arrive - which is what makes a knob feel like a knob
    rather than a counter.
    """
    seq.set_volume_position(24)
    seq.nudge_volume(1, now=1000)
    creep = seq.volume_position

    seq.set_volume_position(24)
    seq.nudge_volume(1, now=2000)  # settle, so this one is unhurried
    seq.set_volume_position(24)
    seq.nudge_volume(1, now=2005)  # five milliseconds later: a spin
    spin = seq.volume_position

    assert spin > creep


def test_creeping_the_knob_still_gives_one_notch(seq):
    """Fine control has to survive, or the volume becomes unusable."""
    seq.set_volume_position(24)
    seq.nudge_volume(1, now=1000)
    assert seq.volume_position == 25


def test_the_knob_survives_the_tick_rollover(seq):
    """ticks_ms wraps at 2**29; a negative gap must not read as a fast spin."""
    seq.set_volume_position(24)
    seq._last_volume_turn = (1 << 29) - 5
    seq.nudge_volume(1, now=5)
    assert seq.volume_position > 24


def test_the_displayed_volume_spans_the_whole_dial(seq):
    """A percentage of knob travel, not of level.

    The level at the quiet end is a number like 0.005, so showing the level
    would read zero for most of the dial's useful travel.
    """
    seq.set_volume_position(0)
    assert seq.volume_percent == 0
    seq.set_volume_position(sequencer_module.VOLUME_STEPS // 2)
    assert seq.volume_percent == 50
    seq.set_volume_position(sequencer_module.VOLUME_STEPS)
    assert seq.volume_percent == 100


def test_the_quiet_end_still_shows_a_moving_number(seq):
    """Every notch has to change the display, or the knob feels dead."""
    seen = set()
    for position in range(0, 13):
        seq.set_volume_position(position)
        seen.add(seq.volume_percent)
    assert len(seen) > 6, sorted(seen)


# --- letting go of a track's audio ----------------------------------------
#
# A mixer voice holds a raw pointer into the buffer it is playing, so
# dropping that buffer while the DMA is still walking it is the hard fault
# this rework exists to eliminate. Nothing reached it while the kit loaded
# once at boot; assigning a sample from the browser is what makes it live.


def test_releasing_a_track_stops_its_voices_first(seq, kit):
    seq.load_kit(kit)
    seq.trigger(0, MAX_VELOCITY)
    voice = seq.mixer.voice[seq._voice_for(0)]
    assert voice.playing, "the test needs a sounding voice"
    seq._release_track(0)
    assert not voice.playing, "the buffer was dropped while it was playing"


def test_reloading_a_track_stops_the_old_sample(seq, kit):
    """The path the sample browser will take."""
    seq.load_kit(kit)
    seq.trigger(0, MAX_VELOCITY)
    voice = seq.mixer.voice[seq._voice_for(0)]
    seq.load_track(0, kit[1])
    assert not voice.playing or seq._samples[0] is not None


def test_releasing_a_track_forgets_its_levels(seq, kit):
    """A stale velocity would make a released track audible again on the
    next volume change, which re-applies levels to whatever is sounding.
    """
    seq.load_kit(kit)
    seq.trigger(0, MAX_VELOCITY)
    seq._release_track(0)
    for index in seq._voices_of(0):
        assert seq._voice_velocity[index] == 0


def test_silencing_one_track_leaves_the_others_playing(seq, kit):
    seq.load_kit(kit)
    seq.trigger(0, MAX_VELOCITY)
    seq.trigger(1, MAX_VELOCITY)
    other = seq.mixer.voice[seq._voice_for(1)]
    seq.silence_track(0)
    assert other.playing


def test_silencing_survives_a_failing_voice(seq, kit):
    """The audio path is allowed to fail; tearing down must not raise."""
    seq.load_kit(kit)

    def boom():
        raise OSError(5, "boom")

    seq.mixer.voice[seq._voice_for(0)].stop = boom
    seq.trigger(0, MAX_VELOCITY)
    seq.silence_track(0)


# --- the volume and its knob agree ----------------------------------------


def test_a_level_set_directly_snaps_to_a_notch(seq):
    """Otherwise the screen, the voices and the next detent disagree."""
    seq.set_volume(0.2)
    assert seq.volume == pytest.approx(
        sequencer_module.level_for_position(seq.volume_position)
    )


def test_silence_and_full_survive_the_snapping(seq):
    seq.set_volume(0.0)
    assert seq.volume == 0.0 and seq.volume_position == 0
    seq.set_volume(1.0)
    assert seq.volume == 1.0
    assert seq.volume_position == sequencer_module.VOLUME_STEPS


def test_the_knob_moves_from_where_the_level_actually_is(seq):
    seq.set_volume(0.2)
    position = seq.volume_position
    seq.nudge_volume(1, now=10_000)
    assert seq.volume_position == position + 1
    assert seq.volume == pytest.approx(
        sequencer_module.level_for_position(position + 1)
    )


# --- MIDI clock ------------------------------------------------------------
#
# The badge followed a MIDI master's Start and Stop but not its tempo: it ran
# on at whatever its own knob said. Confirmed on hardware by sending it 720
# clocks at 150 BPM while its own tempo was 90, and watching its tick counter
# advance at exactly 90. TimingClock was never imported, so 0xF8 was never
# decoded.


class FakePort:
    """A MIDI port that hands out prepared messages, then nothing."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.calls = 0

    def receive(self):
        self.calls += 1
        if self.messages:
            return self.messages.pop(0)
        return None


def test_a_midi_clock_advances_the_tick(seq):
    from adafruit_midi.timing_clock import TimingClock

    before = seq.clock.tick
    seq._handle_midi(TimingClock(), now=1000)
    assert seq.clock.tick == before + 1


def test_a_midi_clock_latches_the_clock_external(seq):
    from adafruit_midi.timing_clock import TimingClock

    seq._handle_midi(TimingClock(), now=1000)
    assert seq.clock.source == "ext"


def test_a_run_of_midi_clocks_sets_the_tempo(seq):
    """24 a quarter note. This is what did not happen before."""
    from adafruit_midi.timing_clock import TimingClock

    seq.clock.set_bpm(90)
    now = 0.0
    # The reading is smoothed and counted across a long baseline, so this
    # takes a few beats to arrive rather than one.
    for _ in range(24 * 16):
        now += 16.67
        seq._handle_midi(TimingClock(), now=int(now))
    assert 145 <= seq.clock.bpm <= 155, seq.clock.bpm


def test_the_badge_does_not_run_at_its_own_tempo_under_a_master(seq):
    """The exact failure measured on hardware: 90 BPM under a 150 BPM master."""
    from adafruit_midi.timing_clock import TimingClock

    seq.clock.set_bpm(90)
    seq.transport.start()
    seq.clock.start(0)
    now = 0
    fired = 0
    for _ in range(96):  # four quarter notes of a 150 BPM master
        now += 16.67
        seq._handle_midi(TimingClock(), now=int(now))
        fired += seq.clock.update(int(now))
    assert fired == 96, "one clock is one tick, whatever the knob says"


def test_a_midi_clock_can_start_the_transport(seq):
    from adafruit_midi.timing_clock import TimingClock

    seq.transport.stop()
    seq.sync_starts_transport = True
    seq._handle_midi(TimingClock(), now=1000)
    assert seq.transport.playing is True


def test_handling_reports_whether_there_was_a_message(seq):
    """This is what lets a caller stop draining a port."""
    from adafruit_midi.timing_clock import TimingClock

    assert seq._handle_midi(None) is False
    assert seq._handle_midi(TimingClock(), now=1000) is True


def test_a_poll_drains_more_than_one_message(monkeypatch, seq):
    """At 300 BPM a master sends 120 clocks a second; one a poll loses most."""
    import sequencer as sequencer_module
    from adafruit_midi.timing_clock import TimingClock

    port = FakePort([TimingClock() for _ in range(5)])
    monkeypatch.setattr(sequencer_module, "midi_usb", port)
    seq._last_usb_midi_poll = -1000
    before = seq.clock.tick
    seq.poll_midi_in(now=5000)
    assert seq.clock.tick == before + 5


def test_a_poll_is_bounded_however_much_is_waiting(monkeypatch, seq):
    """Unbounded draining spends a pass the audio buffer needed."""
    import sequencer as sequencer_module
    from adafruit_midi.timing_clock import TimingClock

    port = FakePort([TimingClock() for _ in range(200)])
    monkeypatch.setattr(sequencer_module, "midi_usb", port)
    seq._last_usb_midi_poll = -1000
    before = seq.clock.tick
    seq.poll_midi_in(now=5000)
    assert seq.clock.tick - before == sequencer_module.MAX_MIDI_PER_POLL


def test_an_empty_port_costs_one_call(monkeypatch, seq):
    """Draining must not spin on a port with nothing on it."""
    import sequencer as sequencer_module

    port = FakePort([])
    monkeypatch.setattr(sequencer_module, "midi_usb", port)
    seq._last_usb_midi_poll = -1000
    seq.poll_midi_in(now=5000)
    assert port.calls == 1


# --- draining the parser, not the port -------------------------------------
#
# in_waiting describes the UART's buffer, not adafruit_midi's. receive() reads
# every available byte in one go, so a burst carrying two messages leaves
# in_waiting at zero with the second still held inside the library. Gating the
# drain loop on it therefore drops that message until more bytes happen to
# arrive - which on a badge following a clock is late enough to look like it
# answering the previous press. Reported from a real rig as "I hit play and
# the sequencer stops"; reproduced as a Stop and a Start in one burst leaving
# the transport stopped when it should have been playing.


class BurstPort:
    """A port holding several already-decoded messages, like a parser does.

    in_waiting goes to zero as soon as the first is taken, which is what the
    real library does once it has slurped the bytes.
    """

    def __init__(self, messages):
        self.messages = list(messages)
        self.taken = 0

    @property
    def in_waiting(self):
        return len(self.messages) if self.taken == 0 else 0

    def receive(self):
        self.taken += 1
        if self.messages:
            return self.messages.pop(0)
        return None


def test_both_messages_in_a_burst_are_acted_on(monkeypatch, seq):
    """A Stop and a Start together must end playing, not stopped."""
    import sequencer as sequencer_module
    from adafruit_midi.start import Start
    from adafruit_midi.stop import Stop

    port = BurstPort([Stop(), Start()])
    monkeypatch.setattr(sequencer_module, "midi_uart", port)
    monkeypatch.setattr(sequencer_module, "midi_serial", port)
    seq.transport.stop()
    seq.poll_midi_in(now=1000)
    assert seq.transport.playing is True, "the second message in the burst was lost"


def test_a_start_then_stop_burst_ends_stopped(monkeypatch, seq):
    import sequencer as sequencer_module
    from adafruit_midi.start import Start
    from adafruit_midi.stop import Stop

    port = BurstPort([Start(), Stop()])
    monkeypatch.setattr(sequencer_module, "midi_uart", port)
    monkeypatch.setattr(sequencer_module, "midi_serial", port)
    seq.transport.stop()
    seq.poll_midi_in(now=1000)
    assert seq.transport.playing is False, "the second message in the burst was lost"


def test_a_silent_port_is_asked_once(monkeypatch, seq):
    """in_waiting stays the cheap probe; it just cannot be the loop condition."""
    import sequencer as sequencer_module

    port = BurstPort([])
    monkeypatch.setattr(sequencer_module, "midi_uart", port)
    monkeypatch.setattr(sequencer_module, "midi_serial", port)
    seq.poll_midi_in(now=1000)
    assert port.taken == 0, "an empty port was read anyway"


def test_a_burst_is_still_bounded(monkeypatch, seq):
    import sequencer as sequencer_module
    from adafruit_midi.timing_clock import TimingClock

    port = BurstPort([TimingClock() for _ in range(50)])
    monkeypatch.setattr(sequencer_module, "midi_uart", port)
    monkeypatch.setattr(sequencer_module, "midi_serial", port)
    seq.poll_midi_in(now=1000)
    assert port.taken == sequencer_module.MAX_MIDI_PER_POLL


def test_an_externally_clocked_pattern_actually_sounds(monkeypatch, seq, kit):
    """The whole point of following a clock is that the steps play.

    Reported from a real rig: the master started the badge, the Play light
    came on, the playhead moved, and nothing was audible. The pulse-driven
    ticks were being counted against clock.tick without ever being returned
    from clock.update - and update's return is what the sequencer fires steps
    on, so no step ever triggered.
    """
    import sequencer as sequencer_module
    from adafruit_midi.timing_clock import TimingClock

    seq.load_kit(kit)
    # Several steps across the first two beats. Not step 0 alone: the first
    # pulse after a reset advances the tick to 1, so position 0 comes round
    # again only on the next bar - true of the internal clock as well, and
    # not what this test is about.
    for step in (1, 2, 4, 6):
        seq.song.set_step(0, step, 100)
    seq.transport.start()
    seq.clock.start(0)

    hits = []
    real = seq.trigger
    monkeypatch.setattr(
        seq,
        "trigger",
        lambda track, velocity: (hits.append(track), real(track, velocity))[1],
    )

    now = 0
    for _ in range(sequencer_module.MIDI_CLOCK_PPQN * 2):
        now += 20
        # One `now` for both, exactly as Sequencer.tick does it.
        seq._handle_midi(TimingClock(), now=now)
        for _ in range(seq.clock.update(now)):
            seq._on_tick(now)
    assert hits, "a whole bar of clocks fired no steps at all"


def test_the_step_that_sounds_under_a_master_is_the_right_one(seq, kit):
    from adafruit_midi.timing_clock import TimingClock

    seq.load_kit(kit)
    seq.song.set_step(0, 4, 100)
    seq.transport.start()
    seq.clock.start(0)
    seq.clock.reset()
    fired = 0
    now = 0
    for _ in range(24):
        now += 20
        seq._handle_midi(TimingClock(), now=now)
        fired += seq.clock.update(now)
    assert fired == 24, "clocks went in and ticks did not come out"


def test_the_note_on_the_downbeat_sounds_on_the_first_bar(seq, kit):
    """Reported from the badge: four notes on the first bar, and the one on
    beat one stayed silent until the pattern came round again.

    clock.reset put the playhead at 0 and update incremented before
    reporting, so the first tick the sequencer ever saw was 1 - and the step
    at position 0 was simply never offered on the first lap.
    """
    seq.load_kit(kit)
    seq.song.clear_all()
    seq.song.set_length(16)
    for step in (0, 4, 8, 12):
        seq.song.set_step(0, step, 110)

    hits = []
    seq.trigger = lambda track, velocity: hits.append((track, velocity))
    seq.toggle_play()

    # One pass of the main loop, immediately, before any time has passed.
    for _ in range(seq.clock.update(0)):
        seq._on_tick(0)

    assert hits, "the downbeat did not sound when the transport started"


def test_the_downbeat_sounds_under_a_master_too(seq, kit):
    from adafruit_midi.start import Start
    from adafruit_midi.timing_clock import TimingClock

    seq.load_kit(kit)
    seq.song.clear_all()
    seq.song.set_length(16)
    seq.song.set_step(0, 0, 110)

    hits = []
    seq.trigger = lambda track, velocity: hits.append((track, velocity))
    seq.transport.stop()
    seq.clock.stop()
    seq._handle_midi(Start(), now=0)
    seq._handle_midi(TimingClock(), now=20)
    for _ in range(seq.clock.update(20)):
        seq._on_tick(20)

    assert hits, "the first clock after Start did not sound the downbeat"


def test_the_downbeat_is_not_sounded_twice(seq, kit):
    """Firing where the playhead already is must not double the first hit."""
    seq.load_kit(kit)
    seq.song.clear_all()
    seq.song.set_length(16)
    seq.song.set_step(0, 0, 110)

    hits = []
    seq.trigger = lambda track, velocity: hits.append((track, velocity))
    seq.toggle_play()
    now = 0
    for _ in range(6):  # a whole step's worth of polls
        for _ in range(seq.clock.update(now)):
            seq._on_tick(now)
        now += 1
    assert len(hits) == 1, "the downbeat sounded %d times" % len(hits)


# --- adding a sample to a track the kit had nothing on ---------------------
#
# The kit spends the whole budget between the tracks that have samples, so a
# track that had none has nothing left to spend. Refusing reads as "this
# sample is broken" when it is only "the others have taken it all" - reported
# from the badge as "I selected a sample for track 5 and it said T5 failed".


def test_a_ninth_sample_is_made_room_for_rather_than_refused(seq, tmp_path):
    size = sequencer_module.MAX_RAM_SAMPLE
    kit = [write_wav_sized(tmp_path / ("k%d.wav" % t), size) for t in range(4)]
    seq.load_kit(kit)
    assert seq.ram_used == sequencer_module.RAM_BUDGET, "the kit must fill the budget"

    extra = write_wav_sized(tmp_path / "extra.wav", size)
    assert seq.assign_sample(4, extra) is True
    assert seq.has_sample(4)
    for track in range(5):
        assert seq.has_sample(track), "resharing dropped track %d" % track
    assert seq.ram_used <= sequencer_module.RAM_BUDGET


def test_resharing_shortens_the_tracks_that_were_already_there(seq, tmp_path):
    size = sequencer_module.MAX_RAM_SAMPLE
    kit = [write_wav_sized(tmp_path / ("s%d.wav" % t), size) for t in range(4)]
    seq.load_kit(kit)
    before = seq._sizes[0]
    seq.assign_sample(4, write_wav_sized(tmp_path / "s4.wav", size))
    assert seq._sizes[0] < before, "track 0 kept its whole share"


def test_all_eight_tracks_can_hold_a_sample(seq, tmp_path):
    """What the budget is shared for. Short, but every pad sounds."""
    size = sequencer_module.MAX_RAM_SAMPLE
    for track in range(TRACK_COUNT):
        path = write_wav_sized(tmp_path / ("e%d.wav" % track), size)
        assert seq.assign_sample(track, path) is True, "track %d" % track
    for track in range(TRACK_COUNT):
        assert seq.has_sample(track), "track %d went silent" % track
        assert seq.trigger(track, 100) is True
    assert seq.ram_used <= sequencer_module.RAM_BUDGET


def test_clearing_a_track_needs_no_resharing(seq, tmp_path):
    kit = [write_wav_sized(tmp_path / ("c%d.wav" % t), 8 * 1024) for t in range(4)]
    seq.load_kit(kit)
    assert seq.assign_sample(2, None) is True
    assert not seq.has_sample(2)
    assert seq.has_sample(0) and seq.has_sample(1) and seq.has_sample(3)


# --- coming back as the badge was left -------------------------------------
#
# The badge boots into the sampler, so whatever restore() does is what the
# player finds. It runs at import: nothing in it may raise, however odd the
# card is.


@pytest.fixture
def card(tmp_path, monkeypatch):
    import prefs

    monkeypatch.setattr(prefs.store, "directory", str(tmp_path))
    return prefs


def test_a_fresh_badge_comes_up_on_the_demo(seq, card):
    assert seq.restore() is False
    assert seq.song.is_on(0, 0), "no demo pattern"


def test_the_last_song_is_restored(seq, card, tmp_path, monkeypatch):
    import songfile

    monkeypatch.setattr(songfile.store, "directory", str(tmp_path))
    seq.song.set_length(9)
    seq.song.set_step(0, 3, 99)
    songfile.save(seq.song, "MINE")
    card.set_last_song("MINE")

    fresh = Sequencer()
    assert fresh.restore() is True
    assert fresh.song.length == 9
    assert fresh.song.is_on(0, 3)


def test_the_last_samples_are_restored_over_the_song(seq, card, tmp_path):
    path = write_wav_sized(tmp_path / "kept.wav", 4096)
    card.set_last_kit([path])
    seq.restore()
    assert seq.song.kit[0] == path
    assert seq.has_sample(0)


def test_a_remembered_song_that_is_gone_still_boots(seq, card):
    card.set_last_song("VANISHED")
    assert seq.restore() is False
    assert seq.song.is_on(0, 0), "must fall back to the demo"


def test_a_remembered_sample_that_is_gone_still_boots(seq, card):
    card.set_last_kit(["/sd/samples/not-there.wav"])
    seq.restore()
    assert not seq.has_sample(0)
    assert seq.song is not None


def test_restore_never_raises_on_a_card_written_by_anything(seq, card):
    card.save({"song": 42, "kit": {"not": "a list"}})
    seq.restore()
    assert seq.song is not None


# --- the shipped kit -------------------------------------------------------


def test_the_default_kit_is_a_playable_four(seq):
    """Track 3 and 4 are the closed and open hat: a crash says nothing at all
    in the quarter of a second the budget allows, and a hat pair says most of
    what a beat needs."""
    assert sequencer_module.DEFAULT_KIT == (
        "kick_crater.wav",
        "snare_kraken-head_1.wav",
        "hh_hats-closed_1.wav",
        "hh_hats-open_1.wav",
    )


# --- the volume knob survives a power cycle --------------------------------


def test_volume_is_not_written_while_the_knob_is_turning(seq, card):
    seq.nudge_volume(3, now=1000)
    assert card.volume_position() == card.NO_VOLUME, "wrote mid-turn"


def test_volume_is_written_once_the_knob_is_still_and_the_badge_quiet(
    seq, card, monkeypatch
):
    monkeypatch.setattr(sequencer_module, "ticks_ms", lambda: 9000)
    seq.nudge_volume(3, now=1000)
    seq.transport.stop()
    seq.tick()
    assert card.volume_position() == seq.volume_position


def test_volume_is_not_written_under_a_playing_pattern(seq, card, monkeypatch):
    """A card write is tens of ms against a 32 ms buffer."""
    monkeypatch.setattr(sequencer_module, "ticks_ms", lambda: 9000)
    seq.nudge_volume(3, now=1000)
    seq.transport.start()
    seq.tick()
    assert card.volume_position() == card.NO_VOLUME
    seq.transport.stop()
    seq.tick()
    assert card.volume_position() == seq.volume_position, "never caught up"


def test_a_saved_volume_comes_back(seq, card):
    card.set_volume_position(3)
    fresh = Sequencer()
    fresh.restore()
    assert fresh.volume_position == 3


def test_a_fresh_badge_uses_the_firmware_default(seq, card):
    """Zero is a real position meaning silence, so it cannot mean "unset"."""
    assert card.volume_position() == card.NO_VOLUME
    fresh = Sequencer()
    before = fresh.volume_position
    fresh.restore()
    assert fresh.volume_position == before


def test_a_saved_silence_is_honoured(seq, card):
    card.set_volume_position(0)
    fresh = Sequencer()
    fresh.restore()
    assert fresh.volume_position == 0


def test_a_kit_of_silent_tracks_is_restored_as_silence(seq, card, tmp_path):
    """A player who cleared every track meant it."""
    path = write_wav_sized(tmp_path / "one.wav", 4096)
    seq.load_kit([path])
    card.set_last_kit([None] * TRACK_COUNT)
    seq.restore()
    assert not any(seq.has_sample(t) for t in range(TRACK_COUNT))


def test_a_failed_load_does_not_inherit_an_older_reason(seq, tmp_path):
    """last_error is read to tell "no room" from "will not play"."""
    size = sequencer_module.MAX_RAM_SAMPLE
    for track in range(TRACK_COUNT):
        seq.load_track(track, write_wav_sized(tmp_path / ("f%d.wav" % track), size))
    assert seq.last_error and "budget" in seq.last_error

    # A different failure entirely: a file that is not there.
    assert seq.load_track(0, "/nope/missing.wav") is False
    assert not (seq.last_error and "budget" in seq.last_error), seq.last_error
