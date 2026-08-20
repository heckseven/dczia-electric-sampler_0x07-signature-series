"""Navigating a tree of settings.

The settings are a hierarchy - Song, Track, Samples, Tools - and the badge
has a 128x32 screen showing three rows, one encoder to move with, and two
buttons to go in and out. This models exactly that: where you are in the
tree, which row is under the cursor, and which three rows are visible.

It is pure logic, so the whole of the navigation can be tested without a
display: no drawing, no key codes, no CircuitPython. What comes out is a
title, a window of labels, and the index of the highlighted one.

A leaf carries a `command` - a string the caller acts on - rather than a
callback, so this module never has to know what saving a song means, and
the tests never have to mock it.
"""

VISIBLE_ROWS = 3

# What a row is marked with. Three things a player needs to see without
# reading: which row the knob is on, which rows lead somewhere rather than
# doing something, and whether the list continues past the edge of a screen
# that only ever shows three of it.
CURSOR = ">"
NO_CURSOR = " "
BRANCH = ">"  # trailing: this row opens another list
MORE_ABOVE = "^"
MORE_BELOW = "v"


class Item:
    """One row: either a submenu, or something that happens."""

    def __init__(self, label, children=None, command=None, value=None):
        self.label = label
        self.children = list(children) if children else None
        self.command = command
        # Free-form, for a caller that needs to know which track a row means.
        self.value = value

    @property
    def is_branch(self):
        return bool(self.children)

    def __repr__(self):
        return "<Item %r>" % self.label


class Menu:
    """Where the player is in the tree, and what they can see."""

    def __init__(self, root, rows=VISIBLE_ROWS):
        self.root = root
        self.rows = rows
        # Each level remembers its own cursor, so going back returns you to
        # the row you came from rather than to the top of the list.
        self._path = [root]
        self._cursor = [0]

    # --- where we are -----------------------------------------------------

    @property
    def node(self):
        return self._path[-1]

    @property
    def items(self):
        return self.node.children or []

    @property
    def depth(self):
        return len(self._path) - 1

    @property
    def title(self):
        return self.node.label

    @property
    def cursor(self):
        return self._cursor[-1]

    @property
    def selected(self):
        items = self.items
        if not items:
            return None
        return items[self.cursor]

    @property
    def path_labels(self):
        """Every label from the root to here, for a breadcrumb."""
        return [node.label for node in self._path]

    def breadcrumb(self, width=21):
        """Where the player is, trimmed to fit one row.

        Depth is unbounded, so the trail is shown from the end backwards -
        the levels nearest the player are the ones worth the space, and the
        root is the one they can most afford to lose sight of.
        """
        labels = self.path_labels[1:] or [self.root.label]
        trail = "/".join(labels)
        if len(trail) <= width:
            return trail
        return "<" + trail[-(width - 1) :]

    # --- moving -----------------------------------------------------------

    def move(self, delta):
        """Move the cursor, stopping at the ends rather than wrapping.

        Wrapping is wrong here: the lists are short and a hand spinning an
        encoder should be able to find the end of one by feel, not sail
        past the last row back to the first.
        """
        items = self.items
        if not items:
            return 0
        position = self.cursor + delta
        if position < 0:
            position = 0
        elif position >= len(items):
            position = len(items) - 1
        self._cursor[-1] = position
        return position

    def enter(self):
        """Go into the highlighted row, or return the command it carries.

        Returns None when it opened a submenu, so a caller can tell "went
        deeper" from "asked for something to happen".
        """
        item = self.selected
        if item is None:
            return None
        if item.is_branch:
            self._path.append(item)
            self._cursor.append(0)
            return None
        return item

    def back(self):
        """Go up a level. Returns False at the root, meaning close me."""
        if len(self._path) == 1:
            return False
        self._path.pop()
        self._cursor.pop()
        return True

    def reset(self):
        """Return to the root, forgetting where the cursor was."""
        self._path = [self.root]
        self._cursor = [0]

    # --- what to draw -----------------------------------------------------

    @property
    def more_above(self):
        return self.offset > 0

    @property
    def more_below(self):
        return self.offset + self.rows < len(self.items)

    @property
    def position(self):
        """Which row of how many, for a list longer than the screen."""
        return (self.cursor + 1, len(self.items))

    @property
    def offset(self):
        """The first visible row, scrolled to keep the cursor on screen."""
        items = self.items
        if len(items) <= self.rows:
            return 0
        top = self.cursor - self.rows // 2
        if top < 0:
            top = 0
        elif top > len(items) - self.rows:
            top = len(items) - self.rows
        return top

    def visible(self):
        """The rows to draw, as (label, highlighted) in screen order."""
        items = self.items
        top = self.offset
        window = items[top : top + self.rows]
        return [
            (item.label, top + index == self.cursor)
            for index, item in enumerate(window)
        ]

    def rendered(self, width=21):
        """The three rows as finished strings, markers and all.

        Kept here rather than in the screen so that what the player sees is
        testable without a display, which is the same reason the rest of
        this module exists.
        """
        items = self.items
        top = self.offset
        rows = []
        for index in range(self.rows):
            position = top + index
            if position >= len(items):
                rows.append("")
                continue
            item = items[position]
            cursor = CURSOR if position == self.cursor else NO_CURSOR
            # An edge row doubles as the scroll hint, so no space is spent on
            # a separate indicator on a screen that shows three rows.
            if index == 0 and self.more_above:
                edge = MORE_ABOVE
            elif index == self.rows - 1 and self.more_below:
                edge = MORE_BELOW
            elif item.is_branch:
                edge = BRANCH
            else:
                edge = " "
            room = width - len(cursor) - 1
            rows.append("%s%-*s%s" % (cursor, room, item.label[:room], edge))
        return rows
