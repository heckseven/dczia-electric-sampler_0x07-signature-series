"""Tests for text drawing split into independent lines.

The point of this module is audible rather than visual: updating the display
pops the amplifier, and the size of the pop tracks the area displayio resends.
A single label spanning the screen makes every change a full-screen resend.
"""

import pytest

import screen
import setup


@pytest.fixture
def text_screen():
    return screen.TextScreen(lines=3)


def test_a_screen_has_a_label_for_every_line(text_screen):
    """One label per line is the whole mechanism: a change resends one line."""
    assert len(text_screen) == 3
    assert len(text_screen.group) == 3


def test_setting_a_line_reports_that_it_changed(text_screen):
    assert text_screen.set_line(0, "hello") is True


def test_setting_a_line_to_the_same_text_changes_nothing(text_screen):
    """A redraw that changes nothing must cost nothing."""
    text_screen.set_line(0, "hello")
    assert text_screen.set_line(0, "hello") is False


def test_lines_are_independent(text_screen):
    text_screen.set_line(0, "one")
    text_screen.set_line(1, "two")
    assert text_screen.line(0) == "one"
    assert text_screen.line(1) == "two"


def test_setting_one_line_leaves_the_others_untouched(text_screen):
    text_screen.set_lines(("a", "b", "c"))
    before = text_screen.group.items[1].text
    text_screen.set_line(0, "changed")
    assert text_screen.group.items[1].text == before


def test_set_lines_reports_change_only_when_something_differs(text_screen):
    assert text_screen.set_lines(("a", "b", "c")) is True
    assert text_screen.set_lines(("a", "b", "c")) is False
    assert text_screen.set_lines(("a", "b", "z")) is True


def test_set_lines_tolerates_fewer_texts_than_lines(text_screen):
    assert text_screen.set_lines(("only one",)) is True
    assert text_screen.line(1) == ""


def test_attaching_shows_the_group_once(text_screen):
    text_screen.attach(setup.display)
    assert setup.display.shown is text_screen.group


def test_clearing_empties_every_line(text_screen):
    text_screen.set_lines(("a", "b", "c"))
    text_screen.clear()
    assert [text_screen.line(i) for i in range(3)] == ["", "", ""]
