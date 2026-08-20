"""The settings tree the player moves through.

Structure only: every leaf carries a command string and nothing else, so
this module knows what the menu offers without knowing how any of it is
done. That keeps the shape of the menu readable in one place, and lets the
whole hierarchy be checked in tests - every leaf reachable, every command
handled - without a screen or a card.

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
SONG_LOAD = "song.load"
SONG_DELETE = "song.delete"

TRACK_FLASHY = "track.flashy"
LENGTH_GLOBAL = "length.global"
LENGTH_TRACK = "length.track"

KIT_SAVE = "kit.save"
KIT_SAVE_AS = "kit.save_as"
KIT_RENAME = "kit.rename"
KIT_LOAD = "kit.load"
KIT_DELETE = "kit.delete"
SAMPLE_TRACK = "sample.track"

TOOL_MIDI = "tool.midi"
TOOL_HID = "tool.hid"


def _tracks(command):
    """One row per track, each carrying its own index."""
    return [
        Item("Track %d" % (track + 1), command=command, value=track)
        for track in range(TRACK_COUNT)
    ]


def build():
    """The settings tree. Built fresh so tests cannot leak state into it."""
    return Item(
        "Settings",
        children=[
            Item(
                "Song",
                children=[
                    Item("Save", command=SONG_SAVE),
                    Item("Save as", command=SONG_SAVE_AS),
                    Item("Rename", command=SONG_RENAME),
                    Item("Load", command=SONG_LOAD),
                    Item("Delete", command=SONG_DELETE),
                ],
            ),
            Item(
                "Track",
                children=[
                    Item("Flashy", command=TRACK_FLASHY),
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
                            Item("Load", command=KIT_LOAD),
                            Item("Delete", command=KIT_DELETE),
                        ],
                    ),
                    Item("Tracks", children=_tracks(SAMPLE_TRACK)),
                ],
            ),
            Item(
                "Tools",
                children=[
                    Item("MIDI controller", command=TOOL_MIDI),
                    Item("USB HID", command=TOOL_HID),
                ],
            ),
        ],
    )


def commands(node=None):
    """Every command the tree can produce, for checking nothing is orphaned."""
    node = node or build()
    found = []
    if node.is_branch:
        for child in node.children:
            found.extend(commands(child))
    elif node.command:
        found.append(node.command)
    return found
