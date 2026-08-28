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
from engine.menu import (
    EMPTY,
    SCROLL_DWELL_MS,
    SCROLL_STEP_MS,
    SCROLL_TAIL_MS,
    Item,
    Menu,
)
from engine.song import TRACK_COUNT


class FakeCatalog:
    """A card, without a card. Counts its reads so deferral can be checked."""

    def __init__(self, songs=("beat",), kits=("kit",), samples=("kick.wav",)):
        self._songs = list(songs)
        self._kits = list(kits)
        self._samples = list(samples)
        self.reads = 0

    def songs(self):
        self.reads += 1
        return [(name, name) for name in self._songs]

    def kits(self):
        self.reads += 1
        return [(name, name) for name in self._kits]

    def samples(self):
        self.reads += 1
        return [(name, "/sd/samples/" + name) for name in self._samples]


def enter_by_label(menu, *labels):
    """Walk down to a row by name.

    By name and not by index, because a menu grows: every one of these tests
    broke the day Division was added to Track, and none of them were about
    where Division sits.
    """
    for label in labels:
        rows = [item.label for item in menu.items]
        assert label in rows, "%s is not in %s" % (label, rows)
        menu.move(rows.index(label) - menu.cursor)
        menu.enter()
    return menu


@pytest.fixture
def menu():
    return Menu(settings.build(FakeCatalog()))


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
    enter_by_label(menu, "Track", "Length")
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


def test_the_top_level_is_the_five_sections(menu):
    """Flashy sits above Tools: the animations belong to no track, and it is
    the one row you go to in order to look at the badge rather than play it."""
    labels = [item.label for item in menu.items]
    assert labels == ["Song", "Track", "Samples", "Flashy", "Tools"]


def test_every_leaf_carries_a_command():
    def walk(node):
        if node.is_branch:
            for child in node.build():
                walk(child)
        else:
            assert node.command, "%s does nothing" % node.label

    walk(settings.build(FakeCatalog()))


def test_every_command_is_unique_except_the_ones_carrying_a_value():
    """A repeated command is a row that does the same thing twice.

    The exceptions are the commands whose value says which thing they mean:
    which track, which song, which sample.
    """
    # Two of everything, so a command that legitimately repeats actually does.
    catalog = FakeCatalog(
        songs=["beat", "riff"], kits=["one", "two"], samples=["kick.wav", "snare.wav"]
    )
    found = settings.commands(settings.build(catalog))
    repeated = {name for name in found if found.count(name) > 1}
    assert repeated == {
        settings.LENGTH_TRACK,
        settings.SAMPLE_TRACK,
        settings.SAMPLE_CLEAR,
        settings.SONG_LOAD,
        settings.SONG_DELETE,
        settings.KIT_LOAD,
        settings.KIT_DELETE,
    }


def test_the_per_track_length_rows_carry_their_track_number(menu):
    enter_by_label(menu, "Track", "Length")
    for track in range(TRACK_COUNT):
        item = menu.items[track + 1]  # after Global
        assert item.label == "Track %d" % (track + 1)
        assert item.value == track, "label and index disagree"


def test_a_sample_row_carries_the_track_it_was_opened_from():
    """The same sample list hangs off eight tracks, so the row has to say which."""
    menu = Menu(settings.build(FakeCatalog()))
    menu.move(2)  # Samples
    menu.enter()
    menu.move(1)  # Tracks
    menu.enter()
    menu.move(3)  # Track 4
    menu.enter()
    picks = [item for item in menu.items if item.command == settings.SAMPLE_TRACK]
    assert picks, "the list offered no samples"
    for item in picks:
        track, path = item.value
        assert track == 3
        assert path.endswith(".wav")


def test_a_sample_list_opens_with_a_way_back_to_silence():
    """A pad with a sample on it otherwise has no way to be emptied."""
    menu = Menu(settings.build(FakeCatalog()))
    menu.move(2)
    menu.enter()
    menu.move(1)
    menu.enter()
    menu.enter()  # Track 1
    first = menu.items[0]
    assert first.label == settings.CLEAR_LABEL
    assert first.command == settings.SAMPLE_CLEAR
    assert first.value == 0


def test_the_saved_songs_are_listed_under_load():
    menu = Menu(settings.build(FakeCatalog(songs=["beat", "riff"])))
    menu.enter()  # Song
    menu.move(3)  # Load
    menu.enter()
    assert [item.label for item in menu.items] == ["beat", "riff"]
    assert [item.value for item in menu.items] == ["beat", "riff"]
    assert all(item.command == settings.SONG_LOAD for item in menu.items)


def test_delete_lists_the_same_songs_under_its_own_command():
    """Sharing the listing but not the command: one loads, the other removes."""
    menu = Menu(settings.build(FakeCatalog(songs=["beat"])))
    menu.enter()
    menu.move(4)  # Delete
    menu.enter()
    assert [item.command for item in menu.items] == [settings.SONG_DELETE]


def test_an_empty_card_is_a_list_saying_so_rather_than_a_dead_row():
    menu = Menu(settings.build(FakeCatalog(songs=[])))
    menu.enter()
    menu.move(3)  # Load
    assert menu.selected.is_branch, "Load stopped looking like a list"
    menu.enter()
    assert menu.depth == 2, "Load did not open"
    assert menu.rendered()[0] == EMPTY


def test_the_card_is_not_read_while_the_tree_is_built():
    """Opening settings has to be cheap: a pattern may be playing."""
    catalog = FakeCatalog(songs=["beat"])
    settings.build(catalog)
    assert catalog.reads == 0, "building the tree listed the card"


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
    enter_by_label(menu, "Track", "Length")
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
    enter_by_label(menu, "Track", "Length")  # nine rows
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


# --- sliding a name that does not fit -------------------------------------
#
# A row has 19 columns for a label and sample filenames routinely exceed that,
# so two different files can draw as the same row. The selected one slides,
# but only once the player has stopped moving - see engine/menu.py.

NAME = "cymbals_crucible-center_1.wav"  # 29 characters, from the shipped kit
ROOM = 19  # what a 21-column row leaves for a label
EXTRA = len(NAME) - ROOM


def scrolling_menu(labels=(NAME,)):
    return Menu(Item("Root", children=[Item(name, command="x") for name in labels]))


def test_a_label_that_fits_never_moves():
    """Most rows are short, and a menu that twitches is worse than one that does not."""
    menu = scrolling_menu(("Save",))
    for elapsed in (0, SCROLL_DWELL_MS, SCROLL_DWELL_MS * 10):
        assert menu.scroll_shift(elapsed, 21) == 0


def test_a_long_label_holds_still_until_the_player_pauses():
    """The dwell is the whole point: motion appears only where someone is looking."""
    menu = scrolling_menu()
    menu.scroll_shift(0, 21)  # first ask starts the clock
    assert menu.scroll_shift(SCROLL_DWELL_MS - 1, 21) == 0


def test_a_long_label_slides_a_character_at_a_time():
    menu = scrolling_menu()
    menu.scroll_shift(0, 21)
    assert menu.scroll_shift(SCROLL_DWELL_MS, 21) == 0
    assert menu.scroll_shift(SCROLL_DWELL_MS + SCROLL_STEP_MS, 21) == 1
    assert menu.scroll_shift(SCROLL_DWELL_MS + SCROLL_STEP_MS * 4, 21) == 4


def test_the_slide_stops_at_the_end_of_the_name():
    """Sliding past the end would scroll the label off the row entirely."""
    menu = scrolling_menu()
    menu.scroll_shift(0, 21)
    at_end = SCROLL_DWELL_MS + SCROLL_STEP_MS * EXTRA
    assert menu.scroll_shift(at_end, 21) == EXTRA
    assert menu.scroll_shift(at_end + SCROLL_TAIL_MS - 1, 21) == EXTRA


def test_the_slide_returns_to_the_start_and_repeats():
    """A name read too slowly comes round again from the beginning."""
    menu = scrolling_menu()
    menu.scroll_shift(0, 21)
    cycle = SCROLL_DWELL_MS + SCROLL_STEP_MS * EXTRA + SCROLL_TAIL_MS
    assert menu.scroll_shift(cycle, 21) == 0
    assert menu.scroll_shift(cycle + SCROLL_DWELL_MS + SCROLL_STEP_MS, 21) == 1


def test_moving_the_cursor_starts_the_wait_again():
    """Otherwise a row would arrive mid-slide, showing the middle of a name."""
    menu = scrolling_menu((NAME, NAME.replace("center", "edge")))
    menu.scroll_shift(0, 21)
    assert menu.scroll_shift(SCROLL_DWELL_MS + SCROLL_STEP_MS * 3, 21) == 3
    menu.move(1)
    now = SCROLL_DWELL_MS + SCROLL_STEP_MS * 3
    assert menu.scroll_shift(now, 21) == 0
    assert menu.scroll_shift(now + SCROLL_DWELL_MS - 1, 21) == 0
    assert menu.scroll_shift(now + SCROLL_DWELL_MS + SCROLL_STEP_MS, 21) == 1


def test_a_rebuilt_list_starts_the_wait_again():
    """A listing can put a different name on the same row without moving.

    Songs and samples are read from the card, so a branch can be rebuilt with
    the same number of rows and different names in them. Measuring the slide
    from the position alone would carry the old phase onto the new name and
    open it halfway through.
    """
    root = Item("Root", children=[Item(NAME, command="x")])
    menu = Menu(root)
    menu.scroll_shift(0, 21)
    moved = SCROLL_DWELL_MS + SCROLL_STEP_MS * 3
    assert menu.scroll_shift(moved, 21) == 3

    root.children = [Item(NAME.replace("center", "edge"), command="x")]
    assert menu.scroll_shift(moved, 21) == 0


def test_forgetting_the_slide_starts_it_over():
    """What a screen being reopened needs; the menu itself is never rebuilt."""
    menu = scrolling_menu()
    menu.scroll_shift(0, 21)
    moved = SCROLL_DWELL_MS + SCROLL_STEP_MS * 3
    assert menu.scroll_shift(moved, 21) == 3

    menu.reset_scroll()
    assert menu.scroll_shift(moved, 21) == 0
    assert menu.scroll_shift(moved + SCROLL_DWELL_MS + SCROLL_STEP_MS, 21) == 1


def test_without_a_clock_a_long_label_is_simply_trimmed():
    """engine/ has no clock of its own; a caller with none gets the old behaviour."""
    menu = scrolling_menu()
    assert menu.scroll_shift(None, 21) == 0
    assert menu.row(0, 21, None) == menu.row(0, 21)


def test_a_sliding_row_still_fills_the_width_and_keeps_its_markers():
    """The row is built by concatenation, so a short slice would shorten the row."""
    menu = scrolling_menu()
    menu.row(0, 21, 0)
    row = menu.row(0, 21, SCROLL_DWELL_MS + SCROLL_STEP_MS * 4)
    assert len(row) == 21
    assert row.startswith(">")
    assert row[1:-1].strip() == NAME[4 : 4 + ROOM]


def test_only_the_selected_row_slides():
    """Two long names moving at once is noise; the cursor says which one matters."""
    menu = scrolling_menu((NAME, NAME))
    menu.row(0, 21, 0)
    later = SCROLL_DWELL_MS + SCROLL_STEP_MS * 4
    assert menu.row(0, 21, later)[1:-1].strip() == NAME[4 : 4 + ROOM]
    assert menu.row(1, 21, later)[1:-1].strip() == NAME[:ROOM]


def test_empty_rows_are_blank_not_missing(small):
    """Three rows always, so the screen does not shuffle as lists change."""
    tiny = Menu(Item("Root", children=[Item("Only", command="x")]))
    assert len(tiny.rendered()) == 3
    assert tiny.rendered()[1] == ""


def test_the_position_tells_you_where_you_are(menu):
    total = len(menu.items)
    assert menu.position == (1, total)
    menu.move(2)
    assert menu.position == (3, total)


# --- depth ----------------------------------------------------------------


def test_the_breadcrumb_names_the_level(menu):
    enter_by_label(menu, "Track")
    assert menu.breadcrumb() == "Track"
    enter_by_label(menu, "Length")
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


# --- branches built from the card -----------------------------------------


def test_a_deferred_branch_is_not_built_until_it_is_opened():
    """Listing a directory costs audio, so it must not happen at build time."""
    calls = []

    def builder():
        calls.append(1)
        return [Item("one"), Item("two")]

    menu = Menu(Item("root", children=[Item("Load", builder=builder)]))
    assert calls == [], "the listing ran while the tree was being built"
    menu.enter()
    assert calls == [1]
    assert [item.label for item in menu.items] == ["one", "two"]


def test_a_deferred_branch_is_built_only_once():
    calls = []

    def builder():
        calls.append(1)
        return [Item("one")]

    menu = Menu(Item("root", children=[Item("Load", builder=builder)]))
    menu.enter()
    menu.back()
    menu.enter()
    assert calls == [1], "reopening re-read the card"


def test_an_unbuilt_deferred_branch_still_looks_like_a_branch():
    """Otherwise the row reads as an action and its marker is missing."""
    item = Item("Load", builder=lambda: [])
    assert item.is_branch is True


def test_a_deferred_branch_that_comes_back_empty_stays_a_branch():
    """An empty folder is a list with nothing in it, not a dead row."""
    item = Item("Load", builder=lambda: [])
    item.build()
    assert item.is_branch is True


def test_an_empty_list_says_so_rather_than_showing_blank_rows():
    menu = Menu(Item("root", children=[Item("Load", builder=lambda: [])]))
    menu.enter()
    assert menu.rendered()[0] == EMPTY


def test_an_empty_list_can_still_be_left():
    menu = Menu(Item("root", children=[Item("Load", builder=lambda: [])]))
    menu.enter()
    assert menu.enter() is None, "there is nothing to select"
    assert menu.back() is True


def test_invalidating_a_branch_makes_the_next_open_read_again():
    """Saving changes what a listing should show."""
    names = ["one"]
    menu = Menu(
        Item("root", children=[Item("Load", builder=lambda: [Item(n) for n in names])])
    )
    menu.enter()
    menu.back()
    names.append("two")
    menu.selected.invalidate()
    menu.enter()
    assert [item.label for item in menu.items] == ["one", "two"]


def test_invalidating_a_fixed_branch_does_nothing():
    """Only card-backed lists are re-read; a static one has nothing to re-read."""
    item = Item("Song", children=[Item("Save")])
    item.invalidate()
    assert [child.label for child in item.children] == ["Save"]


def test_refreshing_rebuilds_the_list_the_cursor_is_standing_in():
    """Forgetting a listing must not empty it under the player."""
    names = ["one", "two"]
    root = Item(
        "root", children=[Item("Load", builder=lambda: [Item(n) for n in names])]
    )
    menu = Menu(root)
    menu.enter()
    assert len(menu.items) == 2
    root.children[0].invalidate()
    menu.refresh()
    assert [item.label for item in menu.items] == ["one", "two"]


def test_refreshing_pulls_the_cursor_back_into_a_shorter_list():
    names = ["one", "two", "three"]
    root = Item(
        "root", children=[Item("Load", builder=lambda: [Item(n) for n in names])]
    )
    menu = Menu(root)
    menu.enter()
    menu.move(2)
    del names[1:]
    root.children[0].invalidate()
    menu.refresh()
    assert menu.cursor == 0
    assert menu.selected.label == "one"


def test_refreshing_a_fixed_list_leaves_the_cursor_alone():
    menu = Menu(Item("root", children=[Item("a"), Item("b"), Item("c")]))
    menu.move(2)
    menu.refresh()
    assert menu.cursor == 2


def test_a_list_that_empties_puts_the_cursor_back_to_the_top():
    """Otherwise the cursor is already out of bounds when rows return."""
    names = ["one", "two", "three"]
    root = Item(
        "root", children=[Item("Load", builder=lambda: [Item(n) for n in names])]
    )
    menu = Menu(root)
    menu.enter()
    menu.move(2)
    del names[:]
    root.children[0].invalidate()
    menu.refresh()
    assert menu.cursor == 0


def test_a_card_too_big_to_list_opens_an_empty_branch_rather_than_failing():
    """The row limit normally prevents this; this is the backstop."""

    def explode():
        raise MemoryError

    menu = Menu(Item("root", children=[Item("Load", builder=explode)]))
    assert menu.enter() is None
    assert menu.depth == 1
    assert menu.items == []


def test_a_row_carries_the_listing_it_came_from():
    """So the screen can find lists to forget by asking, not by counting."""
    menu = Menu(settings.build(FakeCatalog()))
    menu.enter()  # Song
    labels = {item.label: item.kind for item in menu.items}
    assert labels["Load"] == "songs"
    assert labels["Delete"] == "songs"
    assert labels["Save"] is None, "a row that reads nothing claims a listing"


def test_a_long_listing_is_cut_to_what_the_heap_can_hold():
    """Measured on the badge: a row with a filename on it costs 165 bytes."""
    many = ["song%03d" % index for index in range(settings.MAX_ROWS + 40)]
    menu = Menu(settings.build(FakeCatalog(songs=many)))
    menu.enter()
    menu.move(3)  # Load
    menu.enter()
    assert len(menu.items) == settings.MAX_ROWS + 1, "the limit was not applied"


def test_a_cut_listing_says_how_many_are_missing():
    """A list that stops at a round number and says nothing reads as data loss."""
    many = ["song%03d" % index for index in range(settings.MAX_ROWS + 40)]
    menu = Menu(settings.build(FakeCatalog(songs=many)))
    menu.enter()
    menu.move(3)
    menu.enter()
    last = menu.items[-1]
    assert last.command == settings.LIST_TRUNCATED
    assert last.value == 40
    assert "40" in last.label


def test_a_listing_that_fits_has_no_extra_row():
    menu = Menu(settings.build(FakeCatalog(songs=["one", "two"])))
    menu.enter()
    menu.move(3)
    menu.enter()
    assert [item.label for item in menu.items] == ["one", "two"]


def test_the_row_that_empties_a_track_survives_a_cut_sample_list():
    """It is the only way back to silence, so it must not be crowded out."""
    many = ["s%03d.wav" % index for index in range(settings.MAX_ROWS + 40)]
    menu = Menu(settings.build(FakeCatalog(samples=many)))
    menu.move(2)  # Samples
    menu.enter()
    menu.move(1)  # Tracks
    menu.enter()
    menu.enter()  # Track 1
    assert menu.items[0].label == settings.CLEAR_LABEL
    assert len(menu.items) == settings.MAX_ROWS + 1
