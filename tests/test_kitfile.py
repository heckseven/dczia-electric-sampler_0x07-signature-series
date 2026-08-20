"""Tests for keeping kits on the card.

A kit is the eight sample paths a song plays, saved apart from the song so
one set of sounds can be shared by several patterns. Unlike a song there is
no engine object to serialise it, so the coercion that keeps a corrupt file
from raising into the main loop lives in kitfile and is checked here.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
import kitfile
from engine.song import TRACK_COUNT
from store import StoreError


@pytest.fixture
def card(tmp_path, monkeypatch):
    directory = tmp_path / "kits"
    directory.mkdir()
    monkeypatch.setattr(kitfile.store, "directory", str(directory))
    return directory


@pytest.fixture
def paths():
    return ["/sd/samples/kick.wav", "/sd/samples/snare.wav"] + [None] * (
        TRACK_COUNT - 2
    )


def test_a_kit_survives_being_saved_and_loaded(card, paths):
    kitfile.save(paths, "909")
    assert kitfile.load("909") == paths


def test_an_empty_track_comes_back_empty_rather_than_as_a_blank_path(card, paths):
    """ "" would reach open() and raise; None is what an empty track means."""
    kitfile.save(paths, "909")
    assert kitfile.load("909")[2] is None


def test_a_kit_is_listed_by_name(card, paths):
    kitfile.save(paths, "909")
    kitfile.save(paths, "808")
    assert kitfile.kits() == ["808", "909"]


def test_a_kit_can_be_deleted(card, paths):
    kitfile.save(paths, "909")
    assert kitfile.delete("909") is True
    assert kitfile.kits() == []


def test_a_kit_can_be_renamed(card, paths):
    kitfile.save(paths, "909")
    kitfile.rename("909", "house")
    assert kitfile.kits() == ["house"]
    assert kitfile.load("house") == paths


# --- files that are not kits ----------------------------------------------


def test_a_kit_with_no_paths_in_it_loads_as_eight_empty_tracks(card):
    """A file from an older firmware must not raise at boot."""
    kitfile.store.save({"something": "else"}, "odd")
    assert kitfile.load("odd") == [None] * TRACK_COUNT


def test_a_kit_holding_something_other_than_a_list_loads_as_empty(card):
    kitfile.store.save({"paths": 7}, "odd")
    assert kitfile.load("odd") == [None] * TRACK_COUNT


def test_a_kit_with_too_many_paths_keeps_only_the_tracks_that_exist(card):
    kitfile.store.save({"paths": ["a.wav"] * (TRACK_COUNT + 4)}, "odd")
    assert len(kitfile.load("odd")) == TRACK_COUNT


def test_a_kit_with_a_number_where_a_path_should_be_leaves_that_track_empty(card):
    """It would reach open() otherwise, which raises a TypeError, not an OSError."""
    kitfile.store.save({"paths": [17, "kick.wav"]}, "odd")
    loaded = kitfile.load("odd")
    assert loaded[0] is None
    assert loaded[1] == "kick.wav"


def test_a_kit_stored_as_bytes_still_loads(card):
    """msgpack round-trips a str as str, but a foreign writer may not."""
    kitfile.store.save({"paths": [b"kick.wav"]}, "odd")
    assert kitfile.load("odd")[0] == "kick.wav"


def test_loading_a_kit_that_is_not_there_raises(card):
    with pytest.raises(StoreError):
        kitfile.load("nothing")
