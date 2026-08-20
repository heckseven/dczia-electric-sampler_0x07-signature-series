"""Tests for spelling a name with one knob and two buttons.

There is no keyboard: a knob that turns, a button that means yes, and a
button that means no. Finishing needs a third gesture and there is no third
button, so the alphabet carries an end marker as its first entry - one turn
back from A, which is where the hand already is after choosing a letter.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
from engine.naming import ALPHABET, DONE, DONE_LABEL, NameEntry


@pytest.fixture
def entry():
    return NameEntry()


def spell(entry, word):
    """Turn and accept until the word is entered. Mirrors what a hand does."""
    for letter in word:
        target = ALPHABET.index(letter)
        entry.turn(target - ALPHABET.index(entry.letter))
        entry.accept()
    return entry


# --- spelling -------------------------------------------------------------


def test_it_starts_empty_and_on_a_letter(entry):
    assert entry.text == ""
    assert entry.letter == "A"
    assert not entry.at_end_marker


def test_turning_moves_through_the_alphabet(entry):
    entry.turn(1)
    assert entry.letter == "B"
    entry.turn(-1)
    assert entry.letter == "A"


def test_accepting_keeps_the_letter(entry):
    entry.accept()
    assert entry.text == "A"


def test_the_next_letter_starts_from_a_again(entry):
    """Not from where the last one left off: names are not usually a run."""
    spell(entry, "Z")
    assert entry.letter == "A"


def test_a_word_can_be_spelled(entry):
    spell(entry, "BEAT")
    assert entry.text == "BEAT"


def test_the_alphabet_wraps(entry):
    """A ring the hand spins, unlike the settings list which has real ends."""
    entry.turn(-1)
    assert entry.at_end_marker
    entry.turn(-1)
    assert entry.letter == ALPHABET[-1]


# --- finishing and undoing ------------------------------------------------


def test_the_end_marker_is_one_turn_back_from_the_start(entry):
    entry.turn(-1)
    assert entry.at_end_marker


def test_accepting_the_end_marker_finishes(entry):
    spell(entry, "BEAT")
    entry.turn(-ALPHABET.index(entry.letter))
    assert entry.accept() is True
    assert entry.finished
    assert entry.result() == "BEAT"


def test_backspace_rubs_out_the_last_letter(entry):
    spell(entry, "BEAT")
    assert entry.backspace() is True
    assert entry.text == "BEA"


def test_backspace_on_an_empty_name_cancels(entry):
    """No is the only way out, so it has to reach all the way out."""
    assert entry.backspace() is False
    assert entry.cancelled
    assert entry.result() is None


def test_a_cancelled_name_yields_nothing(entry):
    spell(entry, "AB")
    entry.backspace()
    entry.backspace()
    entry.backspace()
    assert entry.cancelled
    assert entry.result() is None


def test_a_name_that_is_only_spaces_is_no_name(entry):
    spell(entry, "  ")
    entry.turn(-ALPHABET.index(entry.letter))
    entry.accept()
    assert entry.result() is None


def test_nothing_happens_after_finishing(entry):
    spell(entry, "A")
    entry.turn(-ALPHABET.index(entry.letter))
    entry.accept()
    entry.turn(5)
    entry.accept()
    entry.backspace()
    assert entry.result() == "A"


# --- what gets drawn ------------------------------------------------------


def test_the_preview_shows_the_letter_being_chosen(entry):
    spell(entry, "BE")
    entry.turn(ALPHABET.index("X") - ALPHABET.index(entry.letter))
    assert entry.preview == "BEX"


def test_the_preview_drops_the_marker(entry):
    spell(entry, "BE")
    entry.turn(-ALPHABET.index(entry.letter))
    assert entry.preview == "BE"


def test_the_end_marker_is_spelled_out(entry):
    """The badge's font is ASCII, so a tick or an arrow draws as a blank box."""
    entry.turn(-ALPHABET.index(entry.letter))
    assert entry.letter_label == DONE_LABEL
    assert DONE not in entry.letter_label


def test_a_space_is_drawn_as_something_visible(entry):
    """An invisible character under the cursor looks like a dead knob."""
    entry.turn(ALPHABET.index(" ") - ALPHABET.index(entry.letter))
    assert entry.letter_label == "_"


def test_every_letter_can_be_drawn():
    """Anything not in the badge's font would appear as a blank box."""
    for letter in ALPHABET:
        if letter == DONE:
            continue
        assert 0x20 <= ord(letter) <= 0x7E, repr(letter)


# --- limits ---------------------------------------------------------------


def test_a_name_stops_at_the_limit():
    entry = NameEntry(max_length=4)
    spell(entry, "ABCD")
    assert entry.full
    entry.accept()
    assert entry.finished, "a full name should finish rather than swallow keys"
    assert entry.result() == "ABCD"


def test_an_existing_name_can_be_edited():
    """Rename starts from what the song is called now, not from blank."""
    entry = NameEntry(initial="BEAT")
    assert entry.text == "BEAT"
    entry.backspace()
    assert entry.text == "BEA"


def test_an_initial_name_is_trimmed_to_the_limit():
    entry = NameEntry(initial="A" * 40, max_length=8)
    assert len(entry.text) == 8
