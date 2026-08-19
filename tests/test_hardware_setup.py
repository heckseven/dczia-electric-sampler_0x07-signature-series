"""Tests pinning the hardware configuration in setup.py.

These guard the pin assignments and bus settings against silent regressions.
Pin numbers are cross-checked against the KiCad netlist in Hardware/Final:
each global label there maps to the Pico pad the firmware must drive.
"""

import setup


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


def test_a_card_that_will_not_mount_is_not_fatal():
    """No card is a normal state; the badge runs from flash."""
    assert setup.sdcard is None or setup.sd_baudrate in setup.SD_BAUDRATES
