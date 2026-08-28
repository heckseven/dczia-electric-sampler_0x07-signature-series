"""Tests for spelling a name with one knob and two buttons.

There is no keyboard: a knob that turns, a button that means yes, and a
button that means no. Finishing needs a third gesture and there is no third
button, so the alphabet carries an end marker as its first entry - one turn
back from A, which is where the hand already is after choosing a letter.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
from engine.naming import ALPHABET, NameEntry


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
    assert entry.letter == "A"
    entry.turn(-1)
    assert entry.letter == ALPHABET[-1]
    entry.turn(1)
    assert entry.letter == "A"


# --- finishing and undoing ------------------------------------------------


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
    entry.finish()
    entry.turn(5)
    entry.accept()
    entry.backspace()
    assert entry.result() == "A"


# --- what gets drawn ------------------------------------------------------


def test_the_preview_shows_the_letter_being_chosen(entry):
    spell(entry, "BE")
    entry.turn(ALPHABET.index("X") - ALPHABET.index(entry.letter))
    assert entry.preview == "BEX"


def test_a_space_is_drawn_as_something_visible(entry):
    """An invisible character under the cursor looks like a dead knob."""
    entry.turn(ALPHABET.index(" ") - ALPHABET.index(entry.letter))
    assert entry.letter_label == "_"


def test_every_letter_can_be_drawn():
    """Anything not in the badge's font would appear as a blank box."""
    for letter in ALPHABET:
        if False:
            continue
        assert 0x20 <= ord(letter) <= 0x7E, repr(letter)


# --- limits ---------------------------------------------------------------


def test_a_name_stops_at_the_limit():
    """Play still finishes and Function still rubs out, so it is not stuck -
    and the preview visibly stops growing, which says why."""
    entry = NameEntry(max_length=4)
    spell(entry, "ABCD")
    assert entry.full
    entry.accept()
    assert entry.text == "ABCD", "it took a fifth letter"
    assert not entry.finished, "a full name should not finish on its own"
    entry.backspace()
    assert entry.text == "ABC", "a full name could not be corrected"


def test_an_existing_name_can_be_edited():
    """Rename starts from what the song is called now, not from blank."""
    entry = NameEntry(initial="BEAT")
    assert entry.text == "BEAT"
    entry.backspace()
    assert entry.text == "BEA"


def test_an_initial_name_is_trimmed_to_the_limit():
    entry = NameEntry(initial="A" * 40, max_length=8)
    assert len(entry.text) == 8


# --- the three gestures ----------------------------------------------------
#
# Play finishes, the encoder's click sets a letter, Function rubs out. The
# end marker that used to live before A is gone: the knob's own click is a
# shorter reach than turning to a hidden entry, and it frees Play to mean
# yes here as it does everywhere else on the badge.


def test_play_finishes_wherever_the_knob_is(entry):
    spell(entry, "HI")
    entry.turn(7)  # somewhere in the middle of the alphabet
    assert entry.finish() is True
    assert entry.finished
    assert entry.result() == "HI"


def test_finishing_does_not_keep_the_letter_under_the_knob(entry):
    spell(entry, "HI")
    entry.turn(3)
    entry.finish()
    assert entry.result() == "HI"


def test_the_click_sets_a_letter_without_finishing(entry):
    assert entry.accept() is False
    assert entry.finished is False
    assert entry.text == "A"


def test_finishing_an_empty_name_yields_nothing(entry):
    entry.finish()
    assert entry.result() is None


def test_the_alphabet_has_no_hidden_entry():
    """Every position is a character that can be drawn."""
    for letter in ALPHABET:
        assert 32 <= ord(letter) < 127, repr(letter)


def test_the_first_letter_is_a():
    assert ALPHABET[0] == "A"
