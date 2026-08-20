import adafruit_displayio_ssd1306
import adafruit_midi
import board
import busio
import digitalio
import displayio
import i2cdisplaybus
import screen
import keypad
import neopixel
import rotaryio
import sdcardio
import storage
import time
import usb_midi

# OLED Screen ( display )
displayio.release_displays()


def free_i2c_bus(scl_pin, sda_pin, tries=9):
    """Clock a stuck slave off the bus before claiming it.

    An I2C slave interrupted part way through returning a byte carries on
    holding SDA low, waiting for the clocks it was promised. The master has
    been reset and knows nothing about it, so its first transaction blocks
    forever - in native code, where a KeyboardInterrupt cannot reach, which
    leaves the badge running but frozen on whatever was last drawn.

    This is why a soft reset would not clear it and unplugging would: only
    removing power resets the display as well. The badge soft resets often
    (any save over USB reloads the code), and a reload mid-frame is exactly
    how a slave gets left mid-byte.

    The cure is the one from the I2C specification: clock SCL until the slave
    finishes the byte it thinks it is sending and releases SDA, then issue a
    STOP so it is back in a known state. Returns True if the bus needed
    freeing, which is worth knowing rather than doing silently.
    """
    with digitalio.DigitalInOut(sda_pin) as sda, digitalio.DigitalInOut(scl_pin) as scl:
        sda.switch_to_input(pull=digitalio.Pull.UP)
        scl.switch_to_input(pull=digitalio.Pull.UP)
        if sda.value:
            return False  # nobody is holding the line; nothing to do

        scl.switch_to_output(value=True)
        for _ in range(tries):
            scl.value = False
            time.sleep(0.000005)
            scl.value = True
            time.sleep(0.000005)
            if sda.value:
                break

        # A STOP condition: SDA rising while SCL is high.
        sda.switch_to_output(value=False)
        scl.value = True
        time.sleep(0.000005)
        sda.value = True
        time.sleep(0.000005)
        return True


i2c_was_stuck = free_i2c_bus(board.GP15, board.GP14)
# A full 128x32 frame is 512 bytes plus overhead, so the CircuitPython
# default of 100 kHz makes every frame cost roughly 50 ms.
# 400 kHz, the SSD1306 datasheet maximum. 1 MHz was tried and measured a
# useful 5 ms faster on the worst stall, and looked clean on this panel, but
# it was running during a run of instability - a dead USB endpoint, an
# OSError out of the audio path - and an out-of-spec bus is not something to
# leave in place while chasing a fault. Worth revisiting deliberately, once
# the badge is stable, rather than as one variable among several.
i2c = busio.I2C(board.GP15, board.GP14, frequency=400_000)
# i2cdisplaybus rather than displayio.I2CDisplay: displayio was split into
# busdisplay, fourwire, epaperdisplay, i2cdisplaybus and paralleldisplaybus
# in CircuitPython 9, and the old names were removed in 10.
display_bus = i2cdisplaybus.I2CDisplayBus(i2c, device_address=0x3C)
display = adafruit_displayio_ssd1306.SSD1306(display_bus, width=128, height=32)

# Neopixels
# auto_write pushes the whole strip on every single pixel assignment, so a
# multi-pixel update costs one full write per pixel. adafruit_led_animation
# also sets auto_write False on this object as soon as any animation is
# constructed, so leaving it True here only made the behaviour inconsistent
# depending on whether an animation had been created yet. Push explicitly.
neopixels = neopixel.NeoPixel(board.GP3, 10, brightness=0.1, auto_write=False)

# Board LED
led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT

# Sync Out
sync_out = digitalio.DigitalInOut(board.GP7)
sync_out.direction = digitalio.Direction.OUTPUT

# Sync In
sync_in = digitalio.DigitalInOut(board.GP6)
sync_in.direction = digitalio.Direction.INPUT

# Buttons
# 0-7 Buttons
# 8 Play
# 9 Function
# 10 Select
# 11 Volume
keys = keypad.KeyMatrix(
    row_pins=(board.GP27, board.GP26, board.GP18),
    column_pins=(board.GP20, board.GP21, board.GP22, board.GP28),
    columns_to_anodes=False,
)

# Setup rotary encoders
select_enc = rotaryio.IncrementalEncoder(board.GP16, board.GP17)
volume_enc = rotaryio.IncrementalEncoder(board.GP4, board.GP5)

# MIDI setup
# GP9 is the receive side of the opto-isolated MIDI IN jack (U5, TLP2361).
# timeout=0 makes reads non-blocking. busio.UART defaults to a one second
# timeout, so a receive() with no MIDI waiting stalls the whole main loop for
# a full second - measured on hardware at 1,000,000 us per call, which drags
# the sequencer to roughly a seventh of its tempo. Anything polling this port
# every pass needs the read to return immediately.
midi_uart = busio.UART(tx=board.GP8, rx=board.GP9, baudrate=31250, timeout=0)
midi_serial_channel = 1
midi_serial = adafruit_midi.MIDI(
    midi_in=midi_uart,
    midi_out=midi_uart,
    in_channel=midi_serial_channel - 1,
    out_channel=midi_serial_channel - 1,
)

midi_usb = adafruit_midi.MIDI(
    midi_in=usb_midi.ports[0], midi_out=usb_midi.ports[1], out_channel=0
)

# Setup the SD card and mount it as /sd.
# sdcardio is built into CircuitPython and is a native driver, so it is both
# faster than the pure-Python adafruit_sdcard and costs nothing on a volume
# with very little room left. Dropping adafruit_sdcard also drops
# adafruit_bus_device, which existed only to support it.
#
# The clock matters a great deal for streaming samples. Measured on this board
# reading a real file, in KB/s:
#
#                    512 byte reads   1024 byte reads
#      8 MHz              184              292
#     24 MHz              207              333
#
# and audio needs 43.1 KB/s per voice at 22050. Cards vary in what they will
# accept, so the fastest clock is tried first and slower ones after, rather
# than assuming any particular card copes.
SD_BAUDRATES = (24_000_000, 16_000_000, 8_000_000)

spi = busio.SPI(board.GP10, board.GP11, board.GP12)
sdcard = None
sd_baudrate = None
sd_error = None
for _baudrate in SD_BAUDRATES:
    try:
        # sdcardio takes the chip-select Pin itself and drives it, unlike
        # adafruit_sdcard which expected a DigitalInOut wrapped around it.
        sdcard = sdcardio.SDCard(spi, board.GP13, baudrate=_baudrate)
        vfs = storage.VfsFat(sdcard)
        storage.mount(vfs, "/sd")
        sd_baudrate = _baudrate
        break
    except OSError as error:
        sd_error = error
        if sdcard is not None:
            try:
                sdcard.deinit()
            except OSError:
                pass
            sdcard = None

if sdcard is None:
    # No card, or none this board can talk to. Everything else still works;
    # samples simply come from flash.
    screen.message(display, "", "No SD Card Found!")
    time.sleep(5)
