"""Tests pinning the hardware configuration in setup.py.

These guard the pin assignments and bus settings against silent regressions.
Pin numbers are cross-checked against the KiCad netlist in Hardware/Final:
each global label there maps to the Pico pad the firmware must drive.
"""

import os

import board
import circuitpython_stubs
import setup

# The same directory the stubs put on the path, so the tests that read the
# firmware's source read the tree actually under test. Computing it from
# __file__ instead means DCZIA_PRODUCTION_DIR silently does not apply, and a
# source-order check then passes against this checkout no matter what the
# other one says - which is exactly the pointless green these tests exist to
# avoid.
from conftest import PRODUCTION_DIR as PRODUCTION


def test_display_i2c_runs_faster_than_the_default():
    """Every frame competes with the audio, which has 32 ms of buffer.

    The CircuitPython default of 100 kHz makes a full frame cost about
    50 ms on its own. Measured on the badge, 1 MHz takes the worst stall
    while scrolling during playback from 26 ms to 21 ms. That is above the
    SSD1306 datasheet maximum of 400 kHz and was verified by eye on this
    panel, so the floor is what this asserts, not the exact number.
    """
    assert setup.i2c.frequency >= 400_000


def test_display_i2c_uses_the_schematic_pins():
    # SCL -> GP15, SDA -> GP14 in dc31.kicad_sch
    assert setup.i2c.scl.name == "GP15"
    assert setup.i2c.sda.name == "GP14"


def test_display_is_the_128x32_panel():
    assert (setup.display.width, setup.display.height) == (128, 32)


def test_neopixels_match_the_front_board():
    assert len(setup.neopixels) == 10
    assert setup.neopixels.pin.name == "GP3"


def test_neopixels_do_not_auto_write():
    """auto_write pushes the whole strip on every single pixel assignment."""
    assert setup.neopixels.auto_write is False


def test_midi_uart_transmits_and_receives():
    """MIDI IN (J5 -> U5 TLP2361 -> GP9) was wired but never opened."""
    assert setup.midi_uart.tx.name == "GP8"
    assert setup.midi_uart.rx.name == "GP9"


def test_midi_uart_runs_at_the_midi_baud_rate():
    assert setup.midi_uart.baudrate == 31250


def test_serial_midi_is_configured_both_ways():
    assert setup.midi_serial.midi_out is setup.midi_uart
    assert setup.midi_serial.midi_in is setup.midi_uart


def test_serial_midi_channel_is_consistent_in_both_directions():
    expected = setup.midi_serial_channel - 1
    assert setup.midi_serial.out_channel == expected
    assert setup.midi_serial.in_channel == expected


def test_key_matrix_covers_twelve_keys():
    """8 pads, Play, Function, and both encoder buttons."""
    rows = len(setup.keys.row_pins)
    columns = len(setup.keys.column_pins)
    assert rows * columns == 12


def test_key_matrix_uses_the_schematic_pins():
    assert [pin.name for pin in setup.keys.row_pins] == ["GP27", "GP26", "GP18"]
    assert [pin.name for pin in setup.keys.column_pins] == [
        "GP20",
        "GP21",
        "GP22",
        "GP28",
    ]


def test_sync_pins_match_the_schematic():
    # SYNC_IN -> GP6 (via Q1 inverter), SYNC_OUT -> GP7 (via OPA341 buffer)
    assert setup.sync_in.pin.name == "GP6"
    assert setup.sync_out.pin.name == "GP7"


def test_the_midi_uart_does_not_block():
    """A blocking read stalls the sequencer.

    busio.UART defaults to a one second timeout. Polling MIDI in every pass of
    the main loop with that default measured 1,000,000 us per call on hardware
    and dropped the sequencer to about a seventh of its tempo.
    """
    assert setup.midi_uart.timeout == 0


def test_the_sd_clock_is_negotiated_fastest_first():
    """Streaming capacity is set by the SD clock, and cards differ.

    Measured on this board: 8 MHz gives 292 KB/s in 1 KB reads and 24 MHz
    gives 333 KB/s, against 43.1 KB/s needed per playing voice. The fastest
    is tried first with slower ones as fallback, so a card that cannot cope
    still mounts rather than failing outright.
    """
    rates = list(setup.SD_BAUDRATES)
    assert rates == sorted(rates, reverse=True), "must actually descend"
    assert rates[-1] <= 8_000_000, "slowest fallback must be conservative"
    assert len(set(rates)) == len(rates), "no duplicate rates"


class FirstReadFails:
    """A card like the 64 GB one: the read after init fails, the rest are fine."""

    def __init__(self, failures=1):
        self.failures = failures
        self.reads = 0

    def readblocks(self, block, buf):
        self.reads += 1
        if self.reads <= self.failures:
            raise OSError(5)


def test_the_first_read_after_init_is_spent_deliberately():
    """Measured on a 64 GB SDXC card, five fresh inits in a row:

        reads of block 0:  EIO ok ok ok ok ok

    storage.mount makes the first read, so without this the mount is what
    fails, at every baudrate in turn, and a working card reports as no card.
    """
    card = FirstReadFails()

    setup.wake(card)

    assert card.reads == 1, "it must actually read, not just intend to"


def test_a_failed_first_read_is_not_raised():
    """The whole point: the failure is expected and must not reach the caller."""
    setup.wake(FirstReadFails())


def test_a_runtime_error_is_swallowed_too():
    """The loop below catches both, because the failure is not always an OSError."""

    class Rude:
        reads = 0

        def readblocks(self, block, buf):
            type(self).reads += 1
            raise RuntimeError("card is sulking")

    setup.wake(Rude())


def test_the_read_buffer_is_not_allocated_per_call():
    """Startup is where the heap is most fragmented; three of these would land
    exactly where VfsFat wants contiguous room."""
    seen = []

    class Recorder:
        def readblocks(self, block, buf):
            seen.append(id(buf))

    setup.wake(Recorder())
    setup.wake(Recorder())

    assert seen[0] == seen[1], "a fresh buffer was allocated for each call"
    assert len(setup._wake_buffer) == 512


def test_a_card_that_reads_fine_is_not_disturbed():
    """One 512-byte read, once, on a card that never needed it."""
    card = FirstReadFails(failures=0)

    setup.wake(card)

    assert card.reads == 1


def test_wake_does_not_retry_past_a_card_that_keeps_failing():
    """It spends one read, not as many as it takes.

    A dead card fails this read too. Hiding more than the single expected
    failure would only postpone the report to storage.mount below, which is
    where it belongs - the loop there records the error and moves on.
    """
    card = FirstReadFails(failures=99)

    setup.wake(card)

    assert card.reads == 1, "it must not retry its way past a dead card"


def test_the_first_read_is_spent_before_the_filesystem_is_built():
    """Pinned by source order, because the stub cannot reach the loop.

    circuitpython_stubs.SDCard always raises on construction, so nothing under
    test ever runs the body of the baudrate loop - deleting the wake() call
    would otherwise pass every test here while breaking the card on hardware.
    """
    source = open(os.path.join(PRODUCTION, "setup.py")).read()

    assert "wake(sdcard)" in source, "the first read is never spent"
    assert (
        source.index("sdcardio.SDCard(")
        < source.index("wake(sdcard)")
        < source.index("storage.VfsFat(")
    ), "wake must run between opening the card and mounting it"


def test_a_card_that_will_not_mount_is_not_fatal():
    """No card is a normal state; the badge runs from flash."""
    assert setup.sdcard is None or setup.sd_baudrate in setup.SD_BAUDRATES


# --- freeing a stuck I2C bus ----------------------------------------------
#
# A slave interrupted part way through returning a byte keeps holding SDA
# low, waiting for the clocks it was promised. The master is reset and knows
# nothing about it, so its next transaction blocks in native code where a
# KeyboardInterrupt cannot reach: the badge keeps running, frozen on whatever
# was last drawn, and only removing power clears it - because only that
# resets the display too. Observed on the badge exactly that way.


def test_a_free_bus_is_left_alone():
    """Clocking a healthy bus is needless traffic on every boot."""
    circuitpython_stubs.DigitalInOut.pulled_high = True
    assert setup.free_i2c_bus(board.GP15, board.GP14) is False


def test_a_held_line_is_clocked_free():
    circuitpython_stubs.DigitalInOut.pulled_high = False
    try:
        assert setup.free_i2c_bus(board.GP15, board.GP14) is True
    finally:
        circuitpython_stubs.DigitalInOut.pulled_high = True


def test_the_bus_is_clocked_enough_to_finish_a_byte():
    """Eight bits plus the acknowledge, which is what the spec calls for."""
    circuitpython_stubs.DigitalInOut.pulled_high = False
    seen = []
    real = circuitpython_stubs.DigitalInOut.__init__

    def watching(self, pin):
        real(self, pin)
        seen.append(self)

    circuitpython_stubs.DigitalInOut.__init__ = watching
    try:
        setup.free_i2c_bus(board.GP15, board.GP14)
        clock = [d for d in seen if d.pin is board.GP15][0]
        lows = [v for v in clock.transitions if v is False]
        assert len(lows) >= 9, "only %d clocks" % len(lows)
    finally:
        circuitpython_stubs.DigitalInOut.__init__ = real
        circuitpython_stubs.DigitalInOut.pulled_high = True


def test_the_pins_are_released_afterwards():
    """The bus is claimed by busio.I2C next; a held pin would fail that."""
    circuitpython_stubs.DigitalInOut.pulled_high = False
    seen = []
    real = circuitpython_stubs.DigitalInOut.__init__

    def watching(self, pin):
        real(self, pin)
        seen.append(self)

    circuitpython_stubs.DigitalInOut.__init__ = watching
    try:
        setup.free_i2c_bus(board.GP15, board.GP14)
        assert seen and all(d.deinited for d in seen)
    finally:
        circuitpython_stubs.DigitalInOut.__init__ = real
        circuitpython_stubs.DigitalInOut.pulled_high = True


def test_recovery_runs_before_the_bus_is_claimed():
    """Doing it afterwards would be too late: claiming it is what blocks."""
    source = open(os.path.join(PRODUCTION, "setup.py")).read()
    assert source.index("free_i2c_bus(board.GP15") < source.index("busio.I2C(")
