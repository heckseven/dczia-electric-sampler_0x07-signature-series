"""The settings tree the player moves through.

Structure only: every leaf carries a command string and nothing else, so
this module knows what the menu offers without knowing how any of it is
done. That keeps the shape of the menu readable in one place, and lets the
whole hierarchy be checked in tests - every leaf reachable, every command
handled - without a screen or a card.

Rows whose contents live on the card - the saved songs, the samples a track
could play - are deferred branches: the tree carries a function that lists
them, and the menu calls it when the row is opened. That keeps a directory
listing, which is tens of milliseconds against a 32 ms audio buffer, off the
path that opens the settings screen while a pattern is playing.

Where those listings come from is a `catalog`, which is any object with
`songs()`, `kits()` and `samples()` returning lists of (label, value). The
firmware passes one backed by the card; the tests pass a dictionary. This
module never opens a file.

Track numbers are one-based on the label because that is how they are
printed on the badge, and zero-based in the value because that is how they
are indexed everywhere else.
"""

from engine.menu import Item
from engine.song import TRACK_COUNT

# Commands. Strings rather than an enum: they are compared in one place and
# printed in another, and a typo in either shows up immediately in a test.
SONG_SAVE = "song.save"
SONG_SAVE_AS = "song.save_as"
SONG_RENAME = "song.rename"
SONG_LOAD = "song.load"  # value: the song's name
SONG_DELETE = "song.delete"  # value: the song's name

# Named for where it used to live, under Track. It is a top level row now:
# the animations have nothing to do with a track, and it is the one thing
# on the badge you go to in order to look at it rather than play it.
TRACK_FLASHY = "track.flashy"
TRACK_DIVISION = "track.division"
LENGTH_GLOBAL = "length.global"
LENGTH_TRACK = "length.track"  # value: track index

KIT_SAVE = "kit.save"
KIT_SAVE_AS = "kit.save_as"
KIT_RENAME = "kit.rename"
KIT_LOAD = "kit.load"  # value: the kit's name
KIT_DELETE = "kit.delete"  # value: the kit's name
SAMPLE_TRACK = "sample.track"  # value: (track index, path)
SAMPLE_CLEAR = "sample.clear"  # value: track index

# The row that stands in for the entries a list would not fit. It carries
# how many were left out, so the browser can say so rather than quietly
# ending early - a list that stops at a round number and says nothing is
# indistinguishable from a card that has lost files.
LIST_TRUNCATED = "list.truncated"  # value: how many are not shown

TOOL_MIDI = "tool.midi"
TOOL_HID = "tool.hid"
TOOL_BRIGHTNESS = "tool.brightness"
TOOL_SCREENSAVER = "tool.screensaver"

# What a row that clears a track's sample is called. Named because the
# firmware and the tests both have to recognise it.
CLEAR_LABEL = "(none)"

# Most rows a card-backed list will hold. Measured on the badge: an Item
# with a filename on it costs 165 bytes, so 128 of them is 21 KB against
# the 58 KB free while a pattern is playing. Four hundred - an ordinary
# number for a sample library - is 66 KB, which is not a slow menu but a
# badge that stops.
#
# The names themselves are cheap by comparison, 37 bytes each, so the limit
# is on rows rather than on the listing: reading what is there and showing
# the first 128 of it is honest, and reading only 128 would make the count
# on the last row a lie.
MAX_ROWS = 128


def _kind(listing):
    """Which catalog listing a row was built from.

    Carried on the Item so the screen can find the lists to forget after a
    save or a delete by asking what they are, rather than by counting its
    way through the tree - which quietly breaks the first time a section is
    reordered.
    """
    return listing


class EmptyCatalog:
    """A card with nothing on it. The default, so build() needs no arguments.

    Not to be confused with the real one in SettingsState, which reads the
    card and remembers what it said.
    """

    def songs(self):
        """[(label, name)] - the saved songs."""
        return []

    def kits(self):
        """[(label, name)] - the saved kits."""
        return []

    def samples(self):
        """[(label, path)] - the samples any track could play."""
        return []


def _tracks(command):
    """One row per track, each carrying its own index."""
    return [
        Item("Track %d" % (track + 1), command=command, value=track)
        for track in range(TRACK_COUNT)
    ]


def _leaves(pairs, command, extra=None, value_of=None):
    """Rows for a listing, bounded, with a row saying what was left out.

    `value_of` turns a listing's value into the row's, so a caller that needs
    a different shape does not have to build a second list of the whole
    listing first. That intermediate cost 6 KB on a 98-sample card, at the
    exact moment the rows themselves were being allocated.
    """
    rows = list(extra) if extra else []
    room = MAX_ROWS - len(rows)
    for index in range(min(room, len(pairs))):
        label, value = pairs[index]
        rows.append(
            Item(label, command=command, value=value_of(value) if value_of else value)
        )
    hidden = len(pairs) - room
    if hidden > 0:
        rows.append(
            Item("(%d more not shown)" % hidden, command=LIST_TRUNCATED, value=hidden)
        )
    return rows


def _named(catalog, listing, command):
    """A deferred branch listing songs or kits."""
    return lambda: _leaves(getattr(catalog, listing)(), command)


def _sample_rows(catalog, track):
    """A deferred branch of every sample, each carrying the track it is for.

    The list opens with a row that empties the track, because a pad with a
    sample on it otherwise has no way back to silence.
    """

    def build():
        clear = [Item(CLEAR_LABEL, command=SAMPLE_CLEAR, value=track)]
        # The catalog's own list is walked directly. Building a second list of
        # (label, (track, path)) first held two copies of a 98-sample listing
        # at once - 6 KB - on the pass that was already allocating 99 rows.
        return _leaves(
            catalog.samples(),
            SAMPLE_TRACK,
            extra=clear,
            value_of=lambda path: (track, path),
        )

    return build


# Which commands carry a track index as their value, so the screen can light
# the pad a row is about. Kept beside the commands rather than in the screen:
# adding a track-specific row and forgetting to light its pad is the sort of
# mistake that is invisible until somebody edits the wrong track.
_TRACK_VALUE = (LENGTH_TRACK, SAMPLE_CLEAR)


def track_of(item):
    """The track a row concerns, or None if it is not about one track.

    Three shapes carry a track: a row whose value is the index, a sample row
    whose value is (index, path), and the branch that stands for one track's
    sample list.
    """
    if item is None:
        return None
    value = item.value
    if item.command == SAMPLE_TRACK:
        # (track, path). Guarded because the value comes back through a
        # command handler and a wrong shape should light nothing, not raise.
        if isinstance(value, tuple) and value and isinstance(value[0], int):
            return value[0]
        return None
    if item.command in _TRACK_VALUE and isinstance(value, int):
        return value
    if item.kind == "samples" and isinstance(value, int):
        return value
    return None


def focused_track(menu):
    """Which track the settings screen is currently about, or None.

    The row first, then the branch it sits in - so the eight sample rows of
    Track 5 all answer 5, and so does the "Track 5" row itself before it is
    opened.
    """
    track = track_of(menu.selected)
    if track is None:
        track = track_of(menu.node)
    return track


def build(catalog=None):
    """The settings tree. Built fresh so tests cannot leak state into it."""
    catalog = catalog or EmptyCatalog()
    return Item(
        "Settings",
        children=[
            Item(
                "Song",
                children=[
                    Item("Save", command=SONG_SAVE),
                    Item("Save as", command=SONG_SAVE_AS),
                    Item("Rename", command=SONG_RENAME),
                    Item(
                        "Load",
                        builder=_named(catalog, "songs", SONG_LOAD),
                        kind="songs",
                    ),
                    Item(
                        "Delete",
                        builder=_named(catalog, "songs", SONG_DELETE),
                        kind="songs",
                    ),
                ],
            ),
            Item(
                "Track",
                children=[
                    Item("Division", command=TRACK_DIVISION),
                    Item(
                        "Length",
                        children=[Item("Global", command=LENGTH_GLOBAL)]
                        + _tracks(LENGTH_TRACK),
                    ),
                ],
            ),
            Item(
                "Samples",
                children=[
                    Item(
                        "Kit",
                        children=[
                            Item("Save", command=KIT_SAVE),
                            Item("Save as", command=KIT_SAVE_AS),
                            Item("Rename", command=KIT_RENAME),
                            Item(
                                "Load",
                                builder=_named(catalog, "kits", KIT_LOAD),
                                kind="kits",
                            ),
                            Item(
                                "Delete",
                                builder=_named(catalog, "kits", KIT_DELETE),
                                kind="kits",
                            ),
                        ],
                    ),
                    Item(
                        "Tracks",
                        children=[
                            Item(
                                "Track %d" % (track + 1),
                                builder=_sample_rows(catalog, track),
                                kind="samples",
                                value=track,
                            )
                            for track in range(TRACK_COUNT)
                        ],
                    ),
                ],
            ),
            Item("Flashy", command=TRACK_FLASHY),
            Item(
                "Tools",
                children=[
                    Item("Brightness", command=TOOL_BRIGHTNESS),
                    Item("Screen text", command=TOOL_SCREENSAVER),
                    Item("MIDI controller", command=TOOL_MIDI),
                    Item("USB HID", command=TOOL_HID),
                ],
            ),
        ],
    )


def commands(node=None):
    """Every command the tree can produce, for checking nothing is orphaned.

    Deferred branches are built as they are walked, so a catalog with
    entries on it is what makes the card-backed commands appear here.
    """
    node = node or build()
    found = []
    if node.is_branch:
        for child in node.build():
            found.extend(commands(child))
    elif node.command:
        found.append(node.command)
    return found
