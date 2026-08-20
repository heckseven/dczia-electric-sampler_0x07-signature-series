"""Tests for the shared text screen.

Every screen in the firmware draws through this class, so its guarantees are
the ones keeping display work out of the audio. Measured on the badge, the
mixer holds 32 ms of audio and one line of text costs about 9 ms to draw
while three cost 43 ms, so the two things worth defending are that unchanged
cells are never rewritten and that a single flush cannot draw the world.
"""

import pytest

import circuitpython_stubs
from screen import TextScreen


class FakeDisplay:
    """A display that only accepts root_group, as CircuitPython 9 onward does.

    show() was removed in 9.0, so a fake that still offers it would let a
    caller keep using it and pass.
    """

    def __init__(self, width=128, height=32):
        self.width = width
        self.height = height
        self.root_group = None

    @property
    def shown(self):
        return self.root_group


@pytest.fixture
def display():
    return FakeDisplay()


@pytest.fixture
def scr(display):
    # Pacing is a separate concern with its own tests below; these are about
    # what gets drawn, not when.
    return TextScreen(display, lines=3, min_interval_ms=0)


def writes(scr):
    return sum(row.writes for row in scr._rows)


# --- geometry, without assuming a font -----------------------------------


def test_columns_come_from_the_font_and_the_panel(scr):
    """21 columns of a 6px glyph on a 128px panel, measured on the badge."""
    assert scr.columns == 21


def test_rows_are_tightened_only_when_they_would_not_fit(display):
    """3 rows of a 12px font need 36px on a 32px panel, so the pitch closes up.

    Overlap is safe because the background is transparent; without the
    tightening the bottom row is cut in half, which is what the badge showed.
    """
    scr = TextScreen(display, lines=3)
    assert scr.pitch == 10
    assert (scr.lines - 1) * scr.pitch + scr.glyph_height <= display.height

    roomy = TextScreen(display, lines=2)
    assert roomy.pitch == roomy.glyph_height, "no need to tighten two rows"


def test_a_different_font_changes_the_geometry(display):
    """Nothing may be hard coded to the builtin font's 6x12 cell."""
    tiny = circuitpython_stubs.BuiltinFont(width=4, height=8)
    scr = TextScreen(display, font=tiny)
    assert scr.columns == 32
    assert scr.pitch == 8, "four rows of 8px fit 32px without tightening"


def test_the_background_is_transparent(scr):
    """Rows overlap at a tight pitch; an opaque row would erase the one above."""
    palette = scr._rows[0].pixel_shader
    assert 0 in palette.transparent


# --- only draw what changed ----------------------------------------------


def test_setting_text_draws_nothing_by_itself(scr):
    before = writes(scr)  # construction blanks every cell once
    scr.set_line(0, "Hello")
    assert writes(scr) == before, "drawing belongs to flush"


def test_flush_draws_the_line(scr):
    scr.set_line(0, "Hello")
    assert scr.flush() == 1
    assert scr.drawn(0) == "Hello"


def test_redrawing_the_same_text_costs_nothing(scr):
    scr.set_line(0, "Hello")
    scr.flush()
    before = writes(scr)
    scr.set_line(0, "Hello")
    assert scr.flush() == 0
    assert writes(scr) == before


def test_only_changed_cells_are_written(scr):
    """A cursor moving is two characters, not a line.

    This is the difference between a scroll costing 0.2 ms and 9 ms.
    """
    scr.set_line(0, " Sampler")
    scr.flush()
    before = writes(scr)
    scr.set_line(0, ">Sampler")
    scr.flush()
    assert writes(scr) - before == 1


def test_a_shortened_line_blanks_the_tail(scr):
    """Leftover characters from a longer line would otherwise stay lit."""
    scr.set_line(0, "MIDI Controller")
    scr.flush()
    scr.set_line(0, "Flashy")
    scr.flush()
    blank = scr._tile(" ")
    for column in range(len("Flashy"), len("MIDI Controller")):
        assert scr._rows[0][column, 0] == blank


def test_text_is_truncated_to_the_panel(scr):
    """Writing past the last column raises on the badge."""
    scr.set_line(0, "x" * 200)
    scr.flush()
    assert len(scr.line(0)) == scr.columns


# --- the flush budget -----------------------------------------------------


def test_flush_draws_one_line_at_a_time_by_default(scr):
    """Three lines at once is 43 ms against a 32 ms buffer; one is 9 ms."""
    scr.set_lines(("a", "b", "c"))
    assert scr.pending == 3
    assert scr.flush() == 1
    assert scr.pending == 2
    assert scr.flush() == 1
    assert scr.flush() == 1
    assert scr.pending == 0


def test_the_budget_is_honoured(scr):
    scr.set_lines(("a", "b", "c"))
    assert scr.flush(budget=2) == 2
    assert scr.pending == 1


def test_flush_all_is_available_for_state_entry(scr):
    """Appearing one line at a time would look broken when a screen opens."""
    scr.set_lines(("a", "b", "c"))
    assert scr.flush_all() == 3
    assert scr.pending == 0


def test_a_line_reads_back_before_it_is_drawn(scr):
    scr.set_line(1, "queued")
    assert scr.line(1) == "queued"
    assert scr.drawn(1) == ""


# --- fonts that do not have every character ------------------------------


def test_a_missing_glyph_falls_back_to_blank(scr):
    """A redraw must not raise over one unusual character."""
    scr.set_line(0, "a☃b")
    scr.flush()
    assert scr._rows[0][1, 0] == scr._tile(" ")


def test_glyph_lookups_are_cached(scr):
    """get_glyph allocates per call, which a full line would do per column."""
    scr._tile("A")
    calls = []
    real = scr.font.get_glyph

    def counted(codepoint):
        calls.append(codepoint)
        return real(codepoint)

    scr.font.get_glyph = counted
    try:
        for _ in range(50):
            scr._tile("A")
    finally:
        scr.font.get_glyph = real
    assert calls == []


# --- the display ----------------------------------------------------------


def test_attaching_shows_the_group(scr, display):
    scr.attach()
    assert display.shown is scr.group


def test_clearing_empties_every_line(scr):
    scr.set_lines(("a", "b", "c"))
    scr.flush_all()
    scr.clear()
    scr.flush_all()
    assert [scr.drawn(i) for i in range(len(scr))] == ["", "", ""]


def test_fits_reports_whether_a_string_needs_truncating(scr):
    assert scr.fits("x" * scr.columns)
    assert not scr.fits("x" * (scr.columns + 1))


# --- geometry that must hold for any line count --------------------------


def test_the_last_row_always_fits_on_the_panel(display):
    """Dividing the height by the line count ignores the cell height.

    At three rows of a 12px font on a 32px panel that happens to land exactly
    on the boundary, which hides the mistake; at five rows it puts the bottom
    row four pixels off the screen.
    """
    for lines in range(1, 9):
        scr = TextScreen(display, lines=lines, min_interval_ms=0)
        bottom = (scr.lines - 1) * scr.pitch + scr.glyph_height
        assert bottom <= display.height, "%d lines runs off the panel" % lines


# --- cells the caller never writes ---------------------------------------


def test_untouched_cells_are_blank_for_any_font(display):
    """The diff treats a cell it has never written as already blank.

    That is only true for free with the builtin font, whose space glyph sits
    at tile zero - the index displayio leaves new tiles at. With any other
    font those cells would show whatever glyph is at index zero, and they
    could never recover: writing a space is skipped as "no change".
    """
    odd = circuitpython_stubs.BuiltinFont(tile_offset=7)
    scr = TextScreen(display, lines=2, font=odd, min_interval_ms=0)
    blank = scr._tile(" ")
    assert blank != 0, "the fixture must not put blank at tile zero"
    for row in scr._rows:
        for column in range(scr.columns):
            assert row[column, 0] == blank


def test_a_cell_past_the_end_of_every_line_stays_blank(display):
    odd = circuitpython_stubs.BuiltinFont(tile_offset=7)
    scr = TextScreen(display, lines=2, font=odd, min_interval_ms=0)
    scr.set_line(0, "hi")
    scr.flush()
    assert scr._rows[0][scr.columns - 1, 0] == scr._tile(" ")


# --- pacing ---------------------------------------------------------------


def test_drawing_is_rate_limited(display):
    """Scrolling fast keeps text changing on every pass.

    Without a floor between draws the screen would draw every pass and never
    leave the 32 ms audio buffer time to refill, which is what mangles the
    sound.
    """
    scr = TextScreen(display, lines=3, min_interval_ms=1000)
    scr.set_line(0, "one")
    assert scr.flush() == 1, "the first draw is not held back"
    scr.set_line(1, "two")
    assert scr.flush() == 0, "a second draw must wait"
    assert scr.pending == 1


def test_entering_a_screen_ignores_the_rate_limit(display):
    scr = TextScreen(display, lines=3, min_interval_ms=1000)
    scr.set_line(0, "one")
    scr.flush()
    scr.set_lines(("a", "b", "c"))
    assert scr.flush_all() == 3


def test_the_rate_limit_costs_nothing_when_nothing_changed(display):
    """A screen with no pending work must not start the clock."""
    scr = TextScreen(display, lines=3, min_interval_ms=1000)
    scr.set_line(0, "one")
    assert scr.flush() == 1
    for _ in range(50):
        assert scr.flush() == 0
    assert scr.pending == 0
