"""Tests for named files of packed data on the card.

Songs and kits both save through store.Store, so what happens when the card
is absent, when a name is not one a filename can carry, and when a write
fails half way is checked once here rather than twice over.
"""

import os

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
from store import MAX_BYTES, Store, StoreError


@pytest.fixture
def store(tmp_path):
    directory = tmp_path / "things"
    directory.mkdir()
    return Store(str(directory), ".thing", kind="thing")


@pytest.fixture
def missing(tmp_path):
    """A store whose directory does not exist yet."""
    return Store(str(tmp_path / "later"), ".thing", kind="thing")


# --- the round trip -------------------------------------------------------


def test_a_dictionary_survives_being_saved_and_loaded(store):
    store.save({"a": 1, "b": [2, 3]}, "one")
    assert store.load("one") == {"a": 1, "b": [2, 3]}


def test_bytes_survive_being_saved(store):
    """A pattern is a bytearray; msgpack is used so it stays compact."""
    store.save({"steps": bytearray([1, 2, 3])}, "one")
    assert bytes(store.load("one")["steps"]) == bytes([1, 2, 3])


def test_saving_twice_replaces_rather_than_duplicates(store):
    store.save({"a": 1}, "one")
    store.save({"a": 2}, "one")
    assert store.names() == ["one"]
    assert store.load("one") == {"a": 2}


# --- listing --------------------------------------------------------------


def test_names_come_back_sorted(store):
    for name in ("cee", "aye", "bee"):
        store.save({}, name)
    assert store.names() == ["aye", "bee", "cee"]


def test_files_with_another_suffix_are_ignored(store):
    store.save({}, "one")
    with open(os.path.join(store.directory, "notes.txt"), "w") as handle:
        handle.write("hello")
    assert store.names() == ["one"]


def test_a_half_written_file_is_not_listed(store):
    """A power cut leaves a .part behind, which is not something to offer."""
    store.save({}, "one")
    with open(store.path_for("two") + ".part", "wb") as handle:
        handle.write(b"\x80")
    assert store.names() == ["one"]


def test_listing_a_missing_directory_is_empty_rather_than_an_error(missing):
    assert missing.names() == []


# --- no card --------------------------------------------------------------


def test_the_directory_is_created_if_it_is_missing(missing):
    assert missing.available() is True
    assert os.path.isdir(missing.directory)


def test_saving_without_a_card_raises_rather_than_returning(tmp_path):
    """Silently losing a save is worse than saying it failed."""
    store = Store(str(tmp_path / "nope" / "deeper"), ".thing")
    with pytest.raises(StoreError):
        store.save({}, "one")


def test_loading_something_that_is_not_there_raises(store):
    with pytest.raises(StoreError):
        store.load("nothing")


def test_deleting_something_that_is_not_there_says_so(store):
    assert store.delete("nothing") is False


def test_deleting_removes_it(store):
    store.save({}, "one")
    assert store.delete("one") is True
    assert store.names() == []


# --- names ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../escape", "a/b", "", " " * 40, "one\x00two", "sl\\ash"],
)
def test_a_name_that_could_escape_the_directory_is_refused(store, name):
    """These paths are written without escaping, so they are checked instead."""
    with pytest.raises(StoreError):
        store.save({}, name)


def test_a_bad_name_cannot_be_loaded_either(store):
    """A listing is trusted no more than a keyboard is."""
    with pytest.raises(StoreError):
        store.load("../../etc/passwd")


def test_a_bad_name_deletes_nothing(store):
    assert store.delete("../one") is False


def test_an_ordinary_name_is_allowed(store):
    store.save({}, "My Beat 2")
    assert store.names() == ["My Beat 2"]


# --- corrupt files --------------------------------------------------------


def test_an_absurdly_large_file_is_refused_before_reading_it(store):
    """Unpacking it would allocate it, and the badge has 264 KB."""
    with open(store.path_for("huge"), "wb") as handle:
        handle.write(b"\x00" * (MAX_BYTES + 1))
    with pytest.raises(StoreError):
        store.load("huge")


def test_a_file_full_of_rubbish_raises_rather_than_escaping(store):
    with open(store.path_for("bent"), "wb") as handle:
        handle.write(b"not msgpack at all")
    with pytest.raises(StoreError):
        store.load("bent")


def test_a_file_holding_something_other_than_a_dictionary_is_refused(store):
    import msgpack

    with open(store.path_for("list"), "wb") as handle:
        msgpack.pack([1, 2, 3], handle)
    with pytest.raises(StoreError):
        store.load("list")


def test_a_failed_write_leaves_the_old_file_readable(store, monkeypatch):
    """The rack switch is a real thing people press mid-save."""
    store.save({"a": 1}, "one")

    def explode(data, handle):
        raise OSError("card full")

    import store as store_module

    monkeypatch.setattr(store_module.msgpack, "pack", explode)
    with pytest.raises(StoreError):
        store.save({"a": 2}, "one")
    assert store.load("one") == {"a": 1}


def test_a_failed_write_does_not_leave_a_temporary_file_behind(store, monkeypatch):
    def explode(data, handle):
        raise OSError("card full")

    import store as store_module

    monkeypatch.setattr(store_module.msgpack, "pack", explode)
    with pytest.raises(StoreError):
        store.save({}, "one")
    assert os.listdir(store.directory) == []


# --- renaming -------------------------------------------------------------


def test_renaming_moves_the_file(store):
    store.save({"a": 1}, "one")
    assert store.rename("one", "two") is True
    assert store.names() == ["two"]
    assert store.load("two") == {"a": 1}


def test_renaming_to_the_same_name_does_nothing(store):
    store.save({"a": 1}, "one")
    assert store.rename("one", "one") is False
    assert store.load("one") == {"a": 1}


def test_renaming_over_an_existing_name_replaces_it(store):
    store.save({"a": 1}, "one")
    store.save({"a": 2}, "two")
    store.rename("one", "two")
    assert store.names() == ["two"]
    assert store.load("two") == {"a": 1}


def test_renaming_something_that_is_not_there_raises(store):
    with pytest.raises(StoreError):
        store.rename("nothing", "something")


def test_renaming_to_a_name_a_file_cannot_carry_raises(store):
    store.save({}, "one")
    with pytest.raises(StoreError):
        store.rename("one", "../two")
    assert store.names() == ["one"], "the original was lost"


def test_renaming_from_a_name_that_is_not_there_keeps_the_destination(store):
    """The card can be edited by a computer between one save and the next.

    Renaming onto an existing name has to remove it first - this filesystem
    has no atomic replace - so doing that before finding out whether there is
    anything to move would destroy a good song to make room for one that does
    not exist.
    """
    store.save({"a": 1}, "keep")
    with pytest.raises(StoreError):
        store.rename("gone", "keep")
    assert store.names() == ["keep"], "the destination was destroyed"
    assert store.load("keep") == {"a": 1}


# --- files a computer put there -------------------------------------------


def test_a_header_claiming_more_than_the_file_holds_is_refused(store):
    """MAX_BYTES bounds the file, not what the file claims about itself.

    This one does not discriminate between the fixed and unfixed code: the
    host's msgpack is written in Python and raises a parse error where
    CircuitPython's, written in C, allocates first. It is here to pin the
    behaviour, not to prove the guard - that is the test below.
    """
    with open(store.path_for("hostile"), "wb") as handle:
        # A fixmap of one: key "a", value = array32 claiming 0xffffffff items.
        handle.write(b"\x81\xa1a\xdd\xff\xff\xff\xff")
    with pytest.raises(StoreError):
        store.load("hostile")


def test_a_file_too_big_to_unpack_raises_a_store_error_not_a_memory_error(
    store, monkeypatch
):
    """An array header claiming four billion entries is five bytes long.

    CircuitPython's unpacker allocates for what the header claims before it
    can find out the entries are not there. MemoryError is neither OSError
    nor any of the parse errors, so without this it escapes every handler up
    to the main loop and drops the badge to the REPL mid-performance - the
    same reason sequencer.py catches it around the WAV reader.
    """
    import store as store_module

    store.save({"a": 1}, "hostile")

    def explode(handle):
        raise MemoryError

    monkeypatch.setattr(store_module.msgpack, "unpack", explode)
    with pytest.raises(StoreError):
        store.load("hostile")


def test_a_song_too_big_to_write_raises_a_store_error_too(store, monkeypatch):
    import store as store_module

    def explode(data, handle):
        raise MemoryError

    monkeypatch.setattr(store_module.msgpack, "pack", explode)
    with pytest.raises(StoreError):
        store.save({"a": 1}, "big")


def test_a_listing_is_bounded(store, monkeypatch):
    """A card is written by a computer; the length of a listing is not ours."""
    import store as store_module

    monkeypatch.setattr(
        store_module.os,
        "listdir",
        lambda directory: ["s%04d.thing" % index for index in range(5000)],
    )
    assert len(store.names()) == store_module.MAX_ENTRIES


def test_a_listing_that_will_not_fit_is_empty_rather_than_fatal(store, monkeypatch):
    import store as store_module

    def explode(directory):
        raise MemoryError

    monkeypatch.setattr(store_module.os, "listdir", explode)
    assert store.names() == []
