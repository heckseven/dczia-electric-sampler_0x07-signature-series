"""Tests for settings that belong to the badge rather than to a song.

Brightness is not part of a song - loading somebody else's pattern must not
change how bright your panel is - so it lives in its own file. Reading and
writing are both best effort: a badge with no card still works, it just
forgets between power-ups.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
import prefs


@pytest.fixture
def card(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs.store, "directory", str(tmp_path))
    return tmp_path


def test_a_fresh_badge_uses_the_default(card):
    assert prefs.brightness() == prefs.DEFAULT_BRIGHTNESS


def test_a_saved_brightness_comes_back(card):
    prefs.set_brightness(25)
    assert prefs.brightness() == 25


def test_saving_one_setting_keeps_the_others(card):
    prefs.save({"brightness": 20, "something": "else"})
    prefs.set_brightness(30)
    assert prefs.load()["something"] == "else"


# --- the ceiling is a power limit, not a taste one -------------------------
#
# Ten NeoPixels at full white is three channels of about 20 mA each, so
# 600 mA, against a 3V3 rail whose only source is the Pico's own regulator
# with no bulk capacitor anywhere.


def test_brightness_cannot_be_pushed_past_the_ceiling(card):
    prefs.set_brightness(100)
    assert prefs.brightness() == prefs.MAX_BRIGHTNESS


def test_brightness_cannot_be_turned_fully_off(card):
    """A dark panel reads as a dead badge."""
    prefs.set_brightness(0)
    assert prefs.brightness() == prefs.MIN_BRIGHTNESS


def test_a_card_written_by_something_else_cannot_exceed_the_ceiling(card):
    prefs.save({"brightness": 9999})
    assert prefs.brightness() == prefs.MAX_BRIGHTNESS


def test_a_brightness_that_is_not_a_number_falls_back(card):
    prefs.save({"brightness": "very bright"})
    assert prefs.brightness() == prefs.DEFAULT_BRIGHTNESS


def test_a_file_holding_something_other_than_settings_is_ignored(card):
    prefs.store.save({"unrelated": 1}, prefs.NAME)
    assert prefs.brightness() == prefs.DEFAULT_BRIGHTNESS


# --- no card ---------------------------------------------------------------


def test_reading_without_a_card_gives_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs.store, "directory", str(tmp_path / "nope" / "deeper"))
    assert prefs.brightness() == prefs.DEFAULT_BRIGHTNESS


def test_saving_without_a_card_says_so_rather_than_raising(tmp_path, monkeypatch):
    """The badge still works; it just forgets between power-ups."""
    monkeypatch.setattr(prefs.store, "directory", str(tmp_path / "nope" / "deeper"))
    assert prefs.set_brightness(20) is False


# --- what the badge says while the lights run ------------------------------


def test_a_fresh_badge_says_nothing(card):
    """It should not arrive with somebody else's words on it."""
    assert prefs.text() == ""


def test_text_survives_being_saved(card):
    prefs.set_text("HELLO DCZIA")
    assert prefs.text() == "HELLO DCZIA"


def test_text_is_cut_to_one_row(card):
    prefs.set_text("X" * 60)
    assert len(prefs.text()) == prefs.MAX_TEXT


def test_characters_the_font_cannot_draw_are_dropped(card):
    """terminalio is ASCII; anything else draws as a blank box."""
    # The characters go; the spaces that flanked them are ASCII and stay.
    prefs.set_text("café — ok")
    assert prefs.text() == "caf  ok"


def test_text_that_is_not_a_string_falls_back(card):
    prefs.save({"text": 42})
    assert prefs.text() == ""


def test_setting_text_keeps_the_brightness(card):
    prefs.set_brightness(30)
    prefs.set_text("BADGE")
    assert prefs.brightness() == 30
    assert prefs.text() == "BADGE"


# --- the chosen animation -------------------------------------------------
#
# The badge boots into the sampler, so the screensaver is what shows the
# choice. Without this it always came back on the first animation in the list
# however the badge had been left.


def test_a_fresh_badge_has_chosen_no_animation(card):
    assert prefs.animation_name() == ""


def test_a_saved_animation_comes_back(card):
    prefs.set_animation("Comet")
    assert prefs.animation_name() == "Comet"


def test_an_animation_that_is_not_a_string_falls_back(card):
    prefs.save({"animation": 7})
    assert prefs.animation_name() == ""


def test_an_animation_name_is_cut_to_one_row(card):
    prefs.set_animation("X" * 60)
    assert len(prefs.animation_name()) == prefs.MAX_ANIMATION


def test_saving_the_animation_keeps_the_other_settings(card):
    prefs.set_brightness(30)
    prefs.set_text("BADGE")
    prefs.set_animation("Sweep")
    assert prefs.brightness() == 30
    assert prefs.text() == "BADGE"
    assert prefs.animation_name() == "Sweep"


# --- coming back as the badge was left -------------------------------------


def test_a_fresh_badge_remembers_no_song(card):
    assert prefs.last_song() == ""
    assert prefs.last_kit() is None


def test_the_last_song_comes_back(card):
    prefs.set_last_song("MYBEAT")
    assert prefs.last_song() == "MYBEAT"


def test_the_last_kit_comes_back(card):
    paths = ["/sd/samples/a.wav", None, "/samples/b.wav"]
    prefs.set_last_kit(paths)
    assert prefs.last_kit()[:3] == ["/sd/samples/a.wav", None, "/samples/b.wav"]


def test_a_kit_that_is_not_a_list_falls_back(card):
    prefs.save({"kit": "nonsense"})
    assert prefs.last_kit() is None


def test_a_kit_entry_that_is_not_a_path_becomes_a_silent_track(card):
    prefs.save({"kit": ["/sd/a.wav", 7, None]})
    assert prefs.last_kit() == ["/sd/a.wav", None, None]


def test_a_kit_longer_than_the_badge_has_tracks_is_cut(card):
    prefs.set_last_kit(["/sd/%d.wav" % i for i in range(40)])
    assert len(prefs.last_kit()) == prefs.MAX_KIT_TRACKS


def test_remembering_the_setup_keeps_the_other_settings(card):
    prefs.set_brightness(30)
    prefs.set_animation("Sweep")
    prefs.set_last_song("BEAT")
    prefs.set_last_kit(["/sd/a.wav"])
    assert prefs.brightness() == 30
    assert prefs.animation_name() == "Sweep"
    assert prefs.last_song() == "BEAT"
    assert prefs.last_kit()[0] == "/sd/a.wav"


# --- "nothing saved" and "saved, all silent" are different answers ---------


def test_a_kit_of_silent_tracks_survives(card):
    """Clearing every track is a decision, not an absence of one."""
    prefs.set_last_kit([None] * 8)
    assert prefs.last_kit() == [None] * 8


def test_forgetting_the_kit_is_not_the_same_as_silencing_it(card):
    prefs.set_last_kit([None] * 8)
    assert prefs.last_kit() is not None
    prefs.set_last_kit(None)
    assert prefs.last_kit() is None, "forgetting must read back as never saved"


def test_a_prefs_file_that_will_not_parse_reads_as_empty(card, monkeypatch):
    """load runs at import via Sequencer.restore; it may never raise.

    StoreError is not the only thing store.load can produce - a deeply nested
    file gives a RecursionError, which it does not convert.
    """

    def explode(name):
        raise RecursionError("too deep")

    monkeypatch.setattr(prefs.store, "load", explode)
    assert prefs.load() == {}
    assert prefs.brightness() == prefs.DEFAULT_BRIGHTNESS
    assert prefs.last_kit() is None
    assert prefs.volume_position() == prefs.NO_VOLUME


def test_a_prefs_file_that_will_not_write_reports_failure(card, monkeypatch):
    def explode(data, name):
        raise RecursionError("too deep")

    monkeypatch.setattr(prefs.store, "save", explode)
    assert prefs.set_brightness(20) is False
