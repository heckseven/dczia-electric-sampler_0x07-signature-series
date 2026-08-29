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

from engine.clock import ticks_diff

VISIBLE_ROWS = 3

# Sliding a name that does not fit.
#
# A row has 19 columns for a label, and a sample library does not care: the
# Kosmo set alone has "cymbals_crucible-center_1.wav" and
# "cymbals_crucible-center_2.wav", which truncate to the same 19 characters
# and leave the browser showing two rows that read identically. Rather than
# shorten the names on the card - they are the player's files, and the badge
# is not the only thing that reads them - the selected row slides sideways so
# the rest can be read.
#
# Only after a pause, though. A list that starts moving the instant the cursor
# lands on a row is harder to scan than one that truncates, because every turn
# of the knob sets something going. Waiting for the player to stop means the
# motion only ever appears where they are actually looking.
#
# The cycle is: hold at the start, slide a character at a time, hold at the
# end, then jump back and repeat - so a name that is read too slowly comes
# round again from the beginning rather than resuming mid-word.
SCROLL_DWELL_MS = 800
SCROLL_STEP_MS = 200
SCROLL_TAIL_MS = 1000

# What a row is marked with. Three things a player needs to see without
# reading: which row the knob is on, which rows lead somewhere rather than
# doing something, and whether the list continues past the edge of a screen
# that only ever shows three of it.
CURSOR = ">"
NO_CURSOR = " "
BRANCH = ">"  # trailing: this row opens another list
MORE_ABOVE = "^"
MORE_BELOW = "v"

# What an empty list says. A branch built from the card can legitimately have
# nothing in it - no songs saved yet - and three blank rows read as a badge
# that has stopped responding rather than as an answer.
EMPTY = "(none)"


class Item:
    """One row: either a submenu, or something that happens."""

    def __init__(
        self, label, children=None, command=None, value=None, builder=None, kind=None
    ):
        self.label = label
        self.children = list(children) if children else None
        self.command = command
        # Free-form, for a caller that needs to know which track a row means.
        self.value = value
        # A branch whose rows are not known until it is opened: the songs on
        # the card, the samples in a folder. Called with no arguments and
        # expected to return a list of Items.
        #
        # Deferred rather than built with the tree because reading a card
        # takes long enough to be heard. The tree is rebuilt whenever the
        # settings screen opens, and doing that would mean a directory
        # listing - tens of milliseconds against a 32 ms audio buffer - on
        # every open, for lists the player usually is not going to look at.
        # This way that cost lands only on the row that needs it, and lands
        # once.
        self.builder = builder
        self.built = False
        # Which listing a deferred branch was built from, so a caller can
        # find the branches to forget by asking what they are rather than by
        # counting its way through the tree.
        self.kind = kind

    @property
    def is_branch(self):
        """Whether opening this row shows another list.

        A builder counts even before it has run, and an empty result still
        counts afterwards: a folder with nothing in it is an empty list to
        look at, not a row that suddenly stops responding.
        """
        return bool(self.children) or self.builder is not None

    def build(self):
        """Populate a deferred branch. Safe to call more than once."""
        if self.builder is not None and not self.built:
            self.children = list(self.builder() or [])
            self.built = True
        return self.children or []

    def invalidate(self):
        """Forget a built list, so the next open reads the card again.

        Saving or deleting changes what a listing should show, and a stale
        one offers a song that is no longer there.
        """
        if self.builder is not None:
            self.children = None
            self.built = False

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
        # Why the last branch opened with no rows, when it was not simply
        # empty. Read by the screen, so a list nobody can explain gets said
        # out loud rather than left looking broken.
        self.last_error = None
        # What the slide is measured from, and what it was measured for.
        # Compared rather than reset by each of move/enter/back/refresh, so a
        # list that rebuilds underneath the cursor restarts the slide too -
        # which is why the anchor carries the label and not just the position.
        # A rebuilt listing can put a different name on the same row without
        # changing the cursor, the offset, or the length of the list.
        self._scroll_anchor = None
        self._scroll_since = None

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
            # Put the cursor back to the top rather than leaving it pointing
            # into a list that no longer has that many rows. Nothing reads it
            # while the list is empty, but the next thing to refill the list
            # would otherwise find it already out of bounds.
            self._cursor[-1] = 0
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
        # Cleared for every enter, not only the ones that build a list. Left
        # set, a single failed sample listing made the next unrelated row the
        # player chose - Save, or a brightness editor - report "Out of memory"
        # on its way to succeeding.
        self.last_error = None
        if item is None:
            return None
        if item.is_branch:
            # Before building: let go of every other card-backed list, so only
            # the one being looked at is ever held. See _forget_other_branches.
            self._forget_other_branches(item)
            try:
                item.build()
            except MemoryError:
                # A card with more on it than the heap can hold. The branch
                # opens empty rather than taking the badge down; the row
                # limit in engine/settings.py is what normally prevents
                # this, and this is the backstop for when it does not.
                #
                # Left unbuilt rather than marked built with no rows. Marking
                # it made the empty list permanent - the row never tried again
                # however much memory was freed afterwards - and a list nobody
                # can explain is worse than a slow one.
                item.invalidate()
                self.last_error = "out of memory"
            self._path.append(item)
            self._cursor.append(0)
            return None
        return item

    def _in_path(self, node):
        for entry in self._path:
            if entry is node:
                return True
        return False

    def _forget_other_branches(self, keep):
        """Drop every built card-backed list except the one being opened.

        A deferred branch holds one Item per row, and a sample list is 98 of
        them at 165 bytes - about 16 KB. All eight track rows build from the
        same listing, and nothing used to let any of them go: walking Track 1
        to Track 8 held eight separate copies and ran the heap out, so the
        later ones opened empty. Measured free memory with the engine and the
        whole UI loaded is 25,856 bytes, which is room for one such list and
        not for two.

        Only the list being looked at is worth keeping. Rebuilding one costs
        allocation and no card read at all, because the catalog behind it is
        cached - see SettingsState.Catalog.

        Walks by identity rather than by label: two branches can carry the
        same name, and the path is what says which one the player is in.
        """
        stack = [self.root]
        while stack:
            node = stack.pop()
            if (
                node.builder is not None
                and node.built
                and node is not keep
                and not self._in_path(node)
            ):
                node.invalidate()
                # Its rows have just gone; there is nothing below to walk.
                continue
            for child in node.children or ():
                stack.append(child)

    def back(self):
        """Go up a level, letting go of the rows being left behind.

        Returns False at the root, meaning close me.

        The rows are dropped because holding them is not free and nothing
        else drops them: a sample list is 99 Items, about 16 KB, and the
        badge has around 21 KB after boot. Left standing, free memory sits
        under main.py's collection floor and the loop collects on every pass
        - 25 ms at a time against a 32 ms audio buffer - which is heard as
        every sample being mangled, long after the menu was closed.

        Rebuilding costs allocation and no card read, because the catalog
        behind the listing is cached in SettingsState.
        """
        if len(self._path) == 1:
            return False
        leaving = self._path.pop()
        self._cursor.pop()
        leaving.invalidate()
        return True

    def refresh(self):
        """Rebuild any deferred branch the cursor is currently inside.

        Invalidating a listing is how a saved or deleted file is noticed, but
        the player may be standing in the very list being forgotten - and a
        forgotten branch has no rows at all, so without this the songs would
        vanish under the cursor and the screen would read empty. The cursor
        is clamped afterwards, because the list may have come back shorter.
        """
        for node in self._path:
            node.build()
        self.move(0)

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

    def scroll_shift(self, now, width=21):
        """How far the selected label has slid, in characters.

        `now` is a millisecond count from whoever has a clock; None means the
        caller has none, which reads as "do not move" and keeps this module
        free of CircuitPython. Returns 0 whenever the label fits, so a caller
        can ask on every pass without checking first.

        Asking has an effect: when the selected row is not the one the slide
        was last measured for, this restarts the wait rather than reporting a
        position for a name that is no longer there. There is no hook to reset
        from - move, enter, back and refresh all change the selection and none
        of them know a slide exists - so the first ask about a new row is what
        starts its clock.
        """
        if now is None:
            return 0
        item = self.selected
        if item is None:
            return 0
        extra = len(item.label) - (width - 2)
        if extra <= 0:
            return 0

        anchor = (self.cursor, self.offset, item.label)
        if anchor != self._scroll_anchor or self._scroll_since is None:
            self._scroll_anchor = anchor
            self._scroll_since = now
            return 0

        travel = extra * SCROLL_STEP_MS
        cycle = SCROLL_DWELL_MS + travel + SCROLL_TAIL_MS
        # Modulo rather than a running counter: the cycle repeats forever and
        # nothing has to be advanced on a schedule, so a screen that is not
        # being drawn costs nothing and rejoins wherever it should be. It is
        # also what makes a backwards clock harmless - Python's % takes the
        # sign of the divisor, so a negative difference still lands inside the
        # cycle rather than slicing a label from a negative index.
        phase = ticks_diff(now, self._scroll_since) % cycle
        if phase < SCROLL_DWELL_MS:
            return 0
        phase -= SCROLL_DWELL_MS
        if phase >= travel:
            return extra
        return phase // SCROLL_STEP_MS

    def reset_scroll(self):
        """Forget where the slide had got to.

        The states are built once and kept, so this Menu outlives any one
        visit to the screen. Without this, leaving mid-slide and coming back
        to the same row - same cursor, same label - matches the anchor and
        carries on from a phase measured minutes ago, so the name arrives
        already halfway through sliding. The wait is the feature; a screen
        has to open showing the start of the name.
        """
        self._scroll_anchor = None
        self._scroll_since = None

    def row(self, index, width=21, now=None):
        """One finished row, markers and all.

        A row at a time rather than a screenful, because the screen draws a
        line per pass of the main loop and building three when one will be
        used is the difference between fitting the audio buffer and not:
        measured on the badge, a screenful is 5.2 ms of the 32 ms a buffer
        lasts.

        Built by concatenation rather than with a format string. That is not
        a micro-optimisation for its own sake - "%s%-*s%s" measured three
        times slower than this on the RP2040, and this is on the scroll
        path, which is the one place the badge cannot afford to be slow.
        """
        items = self.items
        if not items:
            return EMPTY if index == 0 else ""
        position = self.offset + index
        if index >= self.rows or position >= len(items):
            return ""
        item = items[position]
        cursor = CURSOR if position == self.cursor else NO_CURSOR
        # An edge row doubles as the scroll hint, so no space is spent on a
        # separate indicator on a screen that shows three rows.
        if index == 0 and self.more_above:
            edge = MORE_ABOVE
        elif index == self.rows - 1 and self.more_below:
            edge = MORE_BELOW
        elif item.is_branch:
            edge = BRANCH
        else:
            edge = " "
        room = width - 2
        label = item.label
        if len(label) > room:
            shift = self.scroll_shift(now, width) if position == self.cursor else 0
            label = label[shift : shift + room]
        return cursor + label + " " * (room - len(label)) + edge

    def rendered(self, width=21, now=None):
        """Every visible row. For tests and for a screen being entered."""
        return [self.row(index, width, now) for index in range(self.rows)]
