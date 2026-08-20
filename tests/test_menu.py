"""Tests for moving through a tree of settings.

The badge shows three rows, has one encoder to move with, and two buttons
to go in and out. All of that is modelled here as pure logic, so the whole
of the navigation can be checked without a screen: what is highlighted,
which three rows are visible, and what comes back when the player presses
in or out.
"""

import pytest

import circuitpython_stubs  # noqa: F401  (installs the stubs)
from engine import settings
from engine.menu import Item, Menu
from engine.song import TRACK_COUNT


@pytest.fixture
def menu():
    return Menu(settings.build())


@pytest.fixture
def small():
    """A tiny tree, for tests about shape rather than about the real menu."""
    return Menu(
        Item(
            "Root",
            children=[
                Item("One", command="one"),
                Item("Two", children=[Item("Two A", command="two-a")]),
                Item("Three", command="three"),
            ],
        )
    )


# --- moving the cursor ----------------------------------------------------


def test_it_starts_at_the_top(small):
    assert small.cursor == 0
    assert small.selected.label == "One"


def test_the_encoder_moves_the_cursor(small):
    small.move(1)
    assert small.selected.label == "Two"
    small.move(-1)
    assert small.selected.label == "One"


def test_the_cursor_stops_at_the_bottom(small):
    """Wrapping is wrong here: the lists are short, and a hand spinning an
    encoder should find the end by feel rather than sail past it.
    """
    small.move(50)
    assert small.selected.label == "Three"


def test_the_cursor_stops_at_the_top(small):
    small.move(-50)
    assert small.selected.label == "One"


def test_a_big_turn_moves_by_that_much(small):
    small.move(2)
    assert small.selected.label == "Three"


# --- going in and out -----------------------------------------------------


def test_entering_a_leaf_returns_its_command(small):
    item = small.enter()
    assert item is not None and item.command == "one"


def test_entering_a_branch_opens_it_and_returns_nothing(small):
    small.move(1)
    assert small.enter() is None
    assert small.title == "Two"
    assert small.selected.label == "Two A"


def test_back_leaves_a_submenu(small):
    small.move(1)
    small.enter()
    assert small.back() is True
    assert small.title == "Root"


def test_back_at_the_root_asks_to_close(small):
    """Nothing above the root, so the caller should dismiss the menu."""
    assert small.back() is False


def test_going_back_returns_to_the_row_you_came_from(small):
    """Losing your place on every trip in and out makes a tree unusable."""
    small.move(1)
    small.enter()
    small.back()
    assert small.selected.label == "Two"


def test_reset_returns_to_the_top(small):
    small.move(1)
    small.enter()
    small.reset()
    assert small.title == "Root"
    assert small.cursor == 0


# --- what is on screen ----------------------------------------------------


def test_only_three_rows_are_visible(menu):
    assert len(menu.visible()) == 3


def test_a_short_list_shows_all_of_it(small):
    labels = [label for label, _ in small.visible()]
    assert labels == ["One", "Two", "Three"]


def test_exactly_one_row_is_highlighted(menu):
    highlighted = [flag for _, flag in menu.visible() if flag]
    assert len(highlighted) == 1


def test_the_view_scrolls_to_keep_the_cursor_on_screen(menu):
    """Track 8 is the ninth row of Length; it has to be reachable."""
    menu.move(1)  # Track
    menu.enter()
    menu.move(1)  # Length
    menu.enter()
    menu.move(TRACK_COUNT)  # Global plus eight tracks: the last one
    labels = [label for label, _ in menu.visible()]
    assert "Track %d" % TRACK_COUNT in labels
    assert any(flag for _, flag in menu.visible())


def test_the_highlighted_row_is_always_visible(menu):
    menu.move(1)
    menu.enter()
    menu.move(1)
    menu.enter()
    for position in range(TRACK_COUNT + 1):
        menu.move(1 if position else 0)
        shown = [label for label, flag in menu.visible() if flag]
        assert shown == [menu.selected.label]


def test_an_empty_branch_does_not_crash(small):
    empty = Menu(Item("Root", children=[Item("Nothing", children=[])]))
    assert empty.enter() is not None or empty.visible() is not None


# --- the real tree --------------------------------------------------------


def test_the_top_level_is_the_four_sections(menu):
    labels = [item.label for item in menu.items]
    assert labels == ["Song", "Track", "Samples", "Tools"]


def test_every_leaf_carries_a_command():
    def walk(node):
        if node.is_branch:
            for child in node.children:
                walk(child)
        else:
            assert node.command, "%s does nothing" % node.label

    walk(settings.build())


def test_every_command_is_unique_except_the_per_track_ones():
    found = settings.commands()
    repeated = {name for name in found if found.count(name) > 1}
    assert repeated == {settings.LENGTH_TRACK, settings.SAMPLE_TRACK}


def test_the_per_track_rows_carry_their_track_number(menu):
    menu.move(2)  # Samples
    menu.enter()
    menu.move(1)  # Tracks
    menu.enter()
    for track in range(TRACK_COUNT):
        item = menu.items[track]
        assert item.label == "Track %d" % (track + 1)
        assert item.value == track, "label and index disagree"


def test_song_offers_the_five_operations(menu):
    menu.enter()  # Song
    assert [item.label for item in menu.items] == [
        "Save",
        "Save as",
        "Rename",
        "Load",
        "Delete",
    ]


def test_length_offers_global_and_every_track(menu):
    menu.move(1)
    menu.enter()  # Track
    menu.move(1)
    menu.enter()  # Length
    assert len(menu.items) == TRACK_COUNT + 1
    assert menu.items[0].label == "Global"
