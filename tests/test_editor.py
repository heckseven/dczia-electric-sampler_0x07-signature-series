"""Tests for editing a single number with a single knob.

engine.editor is pure logic, so what a turn does to a value - and what
cancelling puts back - is checked here rather than by watching a badge.
"""

import pytest

from engine.editor import Editor


@pytest.fixture
def applied():
    return []


@pytest.fixture
def editor(applied):
    return Editor("Length", 16, 1, 64, apply=applied.append)


def test_a_turn_moves_the_value(editor):
    editor.turn(1, elapsed_ms=300)
    assert editor.value == 17


def test_a_turn_the_other_way_moves_it_back(editor):
    editor.turn(-1, elapsed_ms=300)
    assert editor.value == 15


def test_the_value_stops_at_the_top(editor):
    editor.set(1000)
    assert editor.value == 64


def test_the_value_stops_at_the_bottom(editor):
    editor.set(-5)
    assert editor.value == 1


def test_a_value_outside_the_range_is_pulled_in_at_the_start():
    """A song saved before a limit changed must not open on an illegal value."""
    assert Editor("Length", 900, 1, 64).value == 64


def test_spinning_the_knob_moves_further_than_creeping_it(editor):
    """A 64 step range is a long way at one detent per step."""
    slow = Editor("Length", 16, 1, 64)
    slow.turn(1, elapsed_ms=300)
    fast = Editor("Length", 16, 1, 64)
    fast.turn(1, elapsed_ms=5)
    assert fast.value - 16 > slow.value - 16


def test_the_change_is_applied_as_the_knob_turns(applied, editor):
    """A length is judged by listening, so it has to be audible before commit."""
    editor.turn(1, elapsed_ms=300)
    assert applied == [17]


def test_no_change_applies_nothing(applied, editor):
    editor.set(16)
    assert applied == []


def test_a_turn_of_zero_applies_nothing(applied, editor):
    editor.turn(0)
    assert applied == []


def test_committing_returns_the_value(editor):
    editor.turn(2, elapsed_ms=300)
    assert editor.commit() == editor.value


def test_committing_leaves_the_value_alone(applied, editor):
    editor.turn(1, elapsed_ms=300)
    editor.commit()
    assert editor.value == 17
    assert applied == [17], "committing pushed the value a second time"


def test_cancelling_puts_the_original_back(editor):
    editor.turn(4, elapsed_ms=300)
    editor.cancel()
    assert editor.value == 16


def test_cancelling_applies_the_original_too(applied, editor):
    """The edit was live, so undoing it has to reach the same place."""
    editor.turn(4, elapsed_ms=300)
    editor.cancel()
    assert applied[-1] == 16


def test_cancelling_twice_does_not_undo_a_later_edit(applied, editor):
    """Otherwise a stale editor could overwrite whatever came after it."""
    editor.cancel()
    editor.set(40)
    editor.cancel()
    assert editor.value == 40


def test_the_ends_are_reported_for_the_display(editor):
    assert editor.at_minimum is False
    editor.set(1)
    assert editor.at_minimum is True
    editor.set(64)
    assert editor.at_maximum is True


def test_the_value_is_shown_as_text(editor):
    assert editor.text == "16"


def test_a_formatter_can_name_the_value():
    """Some numbers read better as words: a division, a track."""
    editor = Editor("Track", 0, 0, 7, formatter=lambda v: "T%d" % (v + 1))
    assert editor.text == "T1"


def test_an_editor_without_a_target_still_moves():
    """Nothing to apply to is not an error; the caller reads .value instead."""
    editor = Editor("Length", 16, 1, 64)
    editor.turn(1, elapsed_ms=300)
    assert editor.value == 17


def test_cancelling_cannot_restore_a_value_outside_the_range():
    """A song saved when the limit was higher opens on an illegal value.

    Cancelling has to put back something legal rather than the number that
    was rejected on the way in.
    """
    applied = []
    editor = Editor("Length", 900, 1, 64, apply=applied.append)
    editor.set(20)
    editor.cancel()
    assert editor.value == 64
    assert applied[-1] == 64
