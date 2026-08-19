"""Text on the display, drawn cheaply enough to not interrupt the audio.

Every screen in the firmware draws through this. Drawing text is the single
most disruptive thing the badge does to its own sound, so the rules for doing
it safely belong in one place rather than being rediscovered per screen.

Why it is built this way, measured on the badge at 16 kHz with a pattern
playing. The mixer holds 512 frames, which is 32 ms of audio, so any single
blocking operation longer than that empties the I2S buffer and the sound
tears:

    adafruit_display_text, .text assign, display detached ....  15.1 ms
    adafruit_display_text, one line on screen ...............   37.0 ms
    adafruit_display_text, three lines ......................  117.2 ms
    ------------------------------------------------------------------
    tile write, one character ...............................    0.0 ms
    tile write, one line ....................................    9.3 ms
    tile write, three lines .................................   43.5 ms

adafruit_display_text lays out glyphs in the interpreter, so a scroll detent
cost more than three buffers' worth of audio before a byte reached the I2C
bus. Writing tile indices into a TileGrid instead happens in native code and
is effectively free; what remains is displayio resending the dirty area.

Hence the two rules this class enforces:

    - characters are compared and only changed cells are written, so a cursor
      moving costs two writes rather than a screen
    - `flush` pushes at most one line per call, so no single pass through the
      main loop can dirty more than about 9 ms of work

Callers set text whenever they like and call `flush` once per main loop pass.
The screen converges over the next few passes, with the audio refilled in
between, and nothing has to reason about timing.

Fonts are not assumed. Geometry comes from `get_bounding_box` and glyph
positions from each glyph's own `tile_index`, so any font exposing the
CircuitPython builtin-font interface works, at whatever cell size it has.
"""

import terminalio
from supervisor import ticks_ms

import displayio
from engine.clock import ticks_diff

# Smallest gap between draws. One line costs about 7.5 ms and the audio
# buffer holds 32 ms, so leaving roughly three times the draw cost between
# draws keeps display work near a quarter of the time even when a caller has
# something new on every pass. That is the case that matters: scrolling fast
# keeps text changing continuously, and without a floor here the screen would
# draw on every pass and never give the buffer a chance to refill.
MIN_INTERVAL_MS = 25

FOREGROUND = 0xFFFFFF
BACKGROUND = 0x000000

# The palette entry the transparent background uses. Not to be confused with a
# tile index, which selects a glyph out of the font's bitmap.
BACKGROUND_INDEX = 0


class TextScreen:
    """A grid of characters, one TileGrid per line.

    One grid per line rather than one for the whole screen because displayio
    resends a dirty area per grid: changing a line then costs that line's
    area, not the screen's. It also lets lines sit closer together than the
    font's cell height, which is what makes three rows fit a 32 pixel panel.
    """

    def __init__(
        self,
        display,
        lines=3,
        font=None,
        columns=None,
        pitch=None,
        min_interval_ms=MIN_INTERVAL_MS,
    ):
        self.display = display
        self.min_interval_ms = min_interval_ms
        # None rather than a timestamp: "never drawn" is not an instant, and
        # writing one here would make the first draw wait, or not, depending
        # on where ticks_ms happened to be. It counts from an unspecified
        # point and wraps, so no number means "long ago".
        self._last_draw = None
        self.font = font or terminalio.FONT
        self.lines = lines

        glyph_width, glyph_height = self.font.get_bounding_box()
        self.glyph_width = glyph_width
        self.glyph_height = glyph_height
        self.columns = columns or (display.width // glyph_width)

        if pitch is None:
            # Prefer the font's own cell height, and only tighten if the rows
            # would not otherwise fit. Rows may overlap because the background
            # is transparent, and glyph ink is shorter than its cell.
            if lines * glyph_height <= display.height:
                pitch = glyph_height
            elif lines > 1:
                # Space the tops so the last row's full cell still lands on
                # the panel. Dividing the height by the line count instead
                # ignores the cell height and puts the bottom row partly off
                # the screen, which is only invisible when the numbers happen
                # to divide - at 3 rows of 12px on 32px they do, at 5 they do
                # not.
                pitch = max(1, (display.height - glyph_height) // (lines - 1))
            else:
                pitch = glyph_height
        self.pitch = pitch

        palette = displayio.Palette(2)
        palette[BACKGROUND_INDEX] = BACKGROUND
        palette[1] = FOREGROUND
        # Transparent so a tighter pitch does not let each row erase the
        # descenders of the row above it.
        palette.make_transparent(BACKGROUND_INDEX)

        self.group = displayio.Group()
        self._rows = []
        for index in range(lines):
            grid = displayio.TileGrid(
                self.font.bitmap,
                pixel_shader=palette,
                width=self.columns,
                height=1,
                tile_width=glyph_width,
                tile_height=glyph_height,
                x=0,
                y=index * pitch,
            )
            self.group.append(grid)
            self._rows.append(grid)

        # What callers have asked for, against what is actually on the panel.
        self._wanted = [""] * lines
        self._drawn = [""] * lines
        self._tiles = {}
        # Resolved once, and used whenever a character is not in the font.
        self._blank = self._tile(" ")
        # displayio leaves new tiles at index 0, which is the space glyph only
        # in the builtin font. The diff below assumes untouched cells are
        # blank, and a cell it never touches would otherwise show whatever
        # glyph sits at index 0 forever - it can never self-correct, because
        # writing a space there is skipped as "no change". Paying once here
        # makes that assumption true for any font.
        for row in self._rows:
            for column in range(self.columns):
                row[column, 0] = self._blank

    # --- geometry ---------------------------------------------------------

    def __len__(self):
        return self.lines

    def fits(self, text):
        """Whether a string fits a line without being cut off."""
        return len(text) <= self.columns

    # --- text -------------------------------------------------------------

    def set_line(self, index, text):
        """Ask for a line's text. Returns True if this changed the request.

        Nothing is drawn here. Drawing happens in `flush`, so a caller can
        set every line it likes without deciding how much work that is.
        """
        text = text[: self.columns]
        if text == self._wanted[index]:
            return False
        self._wanted[index] = text
        return True

    def set_lines(self, texts):
        changed = False
        for index in range(min(self.lines, len(texts))):
            if self.set_line(index, texts[index]):
                changed = True
        return changed

    def line(self, index):
        """The text the screen has been asked to show."""
        return self._wanted[index]

    def drawn(self, index):
        """The text actually on the panel, which may lag by a few passes."""
        return self._drawn[index]

    def clear(self):
        """Queue every line blank. Like set_line, this draws nothing itself."""
        for index in range(self.lines):
            self.set_line(index, "")

    # --- drawing ----------------------------------------------------------

    @property
    def pending(self):
        """How many lines are waiting to be drawn."""
        count = 0
        for index in range(self.lines):
            if self._wanted[index] != self._drawn[index]:
                count += 1
        return count

    def flush(self, budget=1, force=False):
        """Push at most `budget` changed lines. Returns how many were drawn.

        Two limits, because they cover different things. The budget bounds a
        single call, so three lines changing never lands in one pass of the
        main loop. The interval bounds the rate, so a caller with something
        new every pass - which is what scrolling quickly looks like - still
        leaves the audio buffer time to refill between draws.

        `force` skips the interval, for a screen being entered rather than
        one competing with playback.
        """
        if self._wanted == self._drawn:
            return 0
        now = ticks_ms()
        if not force and self.min_interval_ms and self._last_draw is not None:
            if ticks_diff(now, self._last_draw) < self.min_interval_ms:
                return 0
        self._last_draw = now
        drawn = 0
        for index in range(self.lines):
            if drawn >= budget:
                break
            if self._wanted[index] != self._drawn[index]:
                self._draw_line(index)
                drawn += 1
        return drawn

    def flush_all(self):
        """Draw everything now, accepting the stall. For state entry.

        A screen appearing one line at a time looks broken, and a state being
        entered is not yet competing with a pattern the user is listening to.
        """
        return self.flush(budget=self.lines, force=True)

    def _draw_line(self, index):
        wanted = self._wanted[index]
        drawn = self._drawn[index]
        row = self._rows[index]
        # Only touch cells that differ. A moving cursor is two writes.
        limit = min(self.columns, max(len(wanted), len(drawn)))
        for column in range(limit):
            new = wanted[column] if column < len(wanted) else " "
            old = drawn[column] if column < len(drawn) else " "
            if new != old:
                row[column, 0] = self._tile(new)
        self._drawn[index] = wanted

    def _tile(self, character):
        """The font's tile index for a character, cached.

        get_glyph allocates a glyph object per call, which is why this is
        worth remembering: a full line would otherwise allocate once per
        column every time it is drawn.
        """
        index = self._tiles.get(character)
        if index is None:
            glyph = self.font.get_glyph(ord(character))
            if glyph is not None:
                index = glyph.tile_index
            elif character == " ":
                # Nothing left to fall back to; a font without a space is
                # broken, but a redraw must not raise over it.
                index = 0
            else:
                index = self._blank
            self._tiles[character] = index
        return index

    # --- display ----------------------------------------------------------

    def attach(self):
        """Take the display. Belongs to entering a state, not to redrawing.

        Every state shows a group of its own, so returning to one means
        taking the display back. Doing it per redraw would resend the whole
        screen for every scroll detent.
        """
        self.display.show(self.group)
