"""Tests for keeping songs on the card.

Songs live on the SD card rather than in onboard flash: writing to CIRCUITPY
from the badge needs a boot.py that remounts it, and that makes the drive
read-only to the host, which breaks dragging samples on and off it.

What matters here is not that a good song round-trips - though it must - but
what happens when the card is absent, full, or holds a file that is not a
song. A sampler with no card should still play; it just cannot keep
anything.
"""

import os

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
import songfile
from engine.song import MAX_VELOCITY, Song


@pytest.fixture
def card(tmp_path, monkeypatch):
    """A card with a songs directory on it."""
    directory = tmp_path / "songs"
    directory.mkdir()
    monkeypatch.setattr(songfile, "SONG_DIR", str(directory))
    return directory


@pytest.fixture
def song():
    song = Song(length=32, division=2, bpm=137)
    song.set_step(0, 0, 100)
    song.set_step(1, 4, MAX_VELOCITY)
    song.set_offset(0, 0, song.max_offset)
    song.kit[0] = "Kick.wav"
    song.toggle_mute(2)
    song.set_track_strength(1, 0.5)
    song.kit_name = "909"
    return song


# --- the round trip -------------------------------------------------------


def test_a_song_survives_being_saved_and_loaded(card, song):
    songfile.save(song, "beat")
    back = songfile.load("beat")
    assert back.length == song.length
    assert back.division == song.division
    assert int(back.bpm) == int(song.bpm)
    assert back.velocity(0, 0) == 100
    assert back.velocity(1, 4) == MAX_VELOCITY
    assert back.offset(0, 0) == song.offset(0, 0)
    assert back.kit[0] == "Kick.wav"
    assert back.muted[2] is True
    assert back.track_strength[1] == 0.5
    assert back.kit_name == "909"


def test_saving_lists_the_song(card, song):
    assert songfile.songs() == []
    songfile.save(song, "beat")
    assert songfile.songs() == ["beat"]


def test_songs_come_back_sorted(card, song):
    for name in ("zulu", "alpha", "mike"):
        songfile.save(song, name)
    assert songfile.songs() == ["alpha", "mike", "zulu"]


def test_saving_twice_replaces_rather_than_duplicates(card, song):
    songfile.save(song, "beat")
    song.set_step(3, 7, 90)
    songfile.save(song, "beat")
    assert songfile.songs() == ["beat"]
    assert songfile.load("beat").velocity(3, 7) == 90


def test_deleting_removes_it(card, song):
    songfile.save(song, "beat")
    assert songfile.delete("beat") is True
    assert songfile.songs() == []


def test_deleting_something_that_is_not_there_is_not_an_error(card):
    assert songfile.delete("never-existed") is False


# --- no card, or a bad one ------------------------------------------------


def test_with_no_card_saving_fails_clearly(tmp_path, monkeypatch, song):
    """The badge still plays without a card; it just cannot keep anything."""
    monkeypatch.setattr(songfile, "SONG_DIR", str(tmp_path / "no" / "such" / "path"))
    with pytest.raises(songfile.SongFileError):
        songfile.save(song, "beat")


def test_with_no_card_the_list_is_empty_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(songfile, "SONG_DIR", str(tmp_path / "absent"))
    assert songfile.songs() == []


def test_the_songs_directory_is_created_if_missing(tmp_path, monkeypatch, song):
    """A card that has never held a song is not a broken card."""
    directory = tmp_path / "songs"
    monkeypatch.setattr(songfile, "SONG_DIR", str(directory))
    songfile.save(song, "beat")
    assert songfile.load("beat").length == song.length


def test_loading_a_song_that_is_not_there(card):
    with pytest.raises(songfile.SongFileError):
        songfile.load("missing")


def test_a_file_that_is_not_a_song_is_refused(card):
    with open(os.path.join(str(card), "junk" + songfile.SUFFIX), "wb") as handle:
        handle.write(b"this is not msgpack at all")
    with pytest.raises(songfile.SongFileError):
        songfile.load("junk")


def test_a_song_file_holding_the_wrong_shape_is_refused(card):
    """msgpack will happily decode a list. A song is a mapping."""
    import msgpack

    with open(os.path.join(str(card), "wrong" + songfile.SUFFIX), "wb") as handle:
        msgpack.pack([1, 2, 3], handle)
    with pytest.raises(songfile.SongFileError):
        songfile.load("wrong")


def test_an_absurdly_large_file_is_refused_before_reading_it(card):
    """Cheaper to refuse than to discover half way through unpacking."""
    path = os.path.join(str(card), "huge" + songfile.SUFFIX)
    with open(path, "wb") as handle:
        handle.write(b"x" * (songfile.MAX_BYTES + 1))
    with pytest.raises(songfile.SongFileError):
        songfile.load("huge")


def test_a_corrupt_song_does_not_take_the_badge_down(card, song):
    """from_dict treats its input as untrusted, so a damaged file loads as a
    slightly wrong song rather than raising into the main loop.
    """
    import msgpack

    data = song.to_dict()
    data["steps"] = [[9999] * 8]
    data["kit"] = [42, None]
    with open(os.path.join(str(card), "bent" + songfile.SUFFIX), "wb") as handle:
        msgpack.pack(data, handle)
    back = songfile.load("bent")
    assert back.velocity(0, 0) <= MAX_VELOCITY
    assert back.kit[0] is None


# --- not losing what is already there -------------------------------------


def test_a_failed_save_leaves_no_half_written_file(card, song, monkeypatch):
    """A rack switch is a real thing people press mid-write."""
    import msgpack

    def explode(obj, handle):
        raise OSError(28, "no space")

    monkeypatch.setattr(msgpack, "pack", explode)
    with pytest.raises(songfile.SongFileError):
        songfile.save(song, "beat")
    assert songfile.songs() == []
    leftovers = [f for f in os.listdir(str(card)) if f.endswith(songfile.TEMP_SUFFIX)]
    assert leftovers == []


def test_a_failed_overwrite_leaves_the_old_song_readable(card, song, monkeypatch):
    """Saving on top of a song must not be able to destroy it and fail."""
    import msgpack

    songfile.save(song, "beat")
    original = songfile.load("beat").length

    def explode(obj, handle):
        raise OSError(28, "no space")

    monkeypatch.setattr(msgpack, "pack", explode)
    with pytest.raises(songfile.SongFileError):
        songfile.save(song, "beat")
    assert songfile.load("beat").length == original


# --- names ---------------------------------------------------------------


def test_a_name_maps_to_one_path(card):
    assert songfile.path_for("beat").endswith("beat" + songfile.SUFFIX)


def test_files_that_are_not_songs_are_ignored(card, song):
    songfile.save(song, "beat")
    for noise in ("notes.txt", "beat.wav", ".hidden"):
        with open(os.path.join(str(card), noise), "wb") as handle:
            handle.write(b"")
    assert songfile.songs() == ["beat"]
