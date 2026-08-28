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
