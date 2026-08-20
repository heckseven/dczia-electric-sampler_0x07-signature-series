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


# --- what the player can see without reading ------------------------------
#
# Three rows on a 128x32 panel, and a tree of unbounded depth behind them.
# The affordances are the whole of the interface: where the knob is, which
# rows lead somewhere, whether the list continues past the edge, and how
# far in you are.


def test_a_row_that_opens_a_list_says_so(menu):
    rows = menu.rendered()
    assert rows[0].endswith(">"), rows[0]


def test_a_row_that_does_something_does_not(menu):
    menu.enter()  # Song, whose rows are all commands
    rows = menu.rendered()
    assert not rows[0].rstrip().endswith(">"), rows[0]


def test_the_cursor_marks_exactly_one_row(menu):
    rows = menu.rendered()
    assert len([row for row in rows if row.startswith(">")]) == 1


def test_a_longer_list_says_there_is_more_below(menu):
    rows = menu.rendered()
    assert rows[-1].endswith("v"), rows


def test_scrolling_down_says_there_is_more_above(menu):
    menu.move(1)
    menu.enter()  # Track
    menu.move(1)
    menu.enter()  # Length, nine rows
    menu.move(6)
    rows = menu.rendered()
    assert rows[0].endswith("^"), rows
    assert rows[-1].endswith("v"), rows


def test_the_bottom_of_a_list_stops_saying_there_is_more(menu):
    menu.move(1)
    menu.enter()
    menu.move(1)
    menu.enter()
    menu.move(50)
    rows = menu.rendered()
    assert not rows[-1].endswith("v"), rows


def test_a_short_list_has_no_scroll_hints(small):
    rows = small.rendered()
    assert not any(row.endswith(("^", "v")) for row in rows), rows


def test_every_row_fits_the_screen(menu):
    for row in menu.rendered(width=21):
        assert len(row) <= 21, row


def test_a_long_label_is_trimmed_rather_than_wrapped():
    long_menu = Menu(Item("Root", children=[Item("A" * 60, command="x")]))
    rows = long_menu.rendered(width=21)
    assert len(rows[0]) == 21


def test_empty_rows_are_blank_not_missing(small):
    """Three rows always, so the screen does not shuffle as lists change."""
    tiny = Menu(Item("Root", children=[Item("Only", command="x")]))
    assert len(tiny.rendered()) == 3
    assert tiny.rendered()[1] == ""


def test_the_position_tells_you_where_you_are(menu):
    assert menu.position == (1, 4)
    menu.move(2)
    assert menu.position == (3, 4)


# --- depth ----------------------------------------------------------------


def test_the_breadcrumb_names_the_level(menu):
    menu.move(1)
    menu.enter()
    assert menu.breadcrumb() == "Track"
    menu.move(1)
    menu.enter()
    assert menu.breadcrumb() == "Track/Length"


def test_the_breadcrumb_is_trimmed_from_the_far_end():
    """Depth is unbounded; the levels nearest the player are worth the space."""
    deep = Item("Leaf", command="x")
    for name in ("Fifth", "Fourth", "Third", "Second", "First"):
        deep = Item(name, children=[deep])
    menu = Menu(deep)
    for _ in range(4):
        menu.enter()
    crumb = menu.breadcrumb(width=21)
    assert len(crumb) <= 21
    assert crumb.startswith("<")
    assert crumb.endswith("Fifth")


def test_nesting_is_not_limited_to_the_levels_the_settings_happen_to_use():
    """The tree is a stack, so six levels work exactly like two."""
    leaf = Item("Bottom", command="deep")
    node = leaf
    for level in range(6):
        node = Item("Level %d" % level, children=[node])
    menu = Menu(node)
    # Five levels of list, then the leaf at the bottom of them.
    for _ in range(5):
        assert menu.enter() is None, "should have opened another list"
    assert menu.enter().command == "deep"
    for _ in range(5):
        assert menu.back() is True
    assert menu.back() is False, "back at the root should ask to close"
