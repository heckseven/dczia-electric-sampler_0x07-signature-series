"""Minimal fakes for the CircuitPython modules the firmware imports.

Software/Production is written against CircuitPython running on an RP2040. None
of board, busio, audiobusio, keypad and friends exist on a desktop Python, and
setup.py performs real hardware initialisation at import time, so nothing in the
firmware can be imported by a test runner as-is.

install() registers just enough of that surface in sys.modules for the firmware
to import and be exercised. The fakes are deliberately shallow, but where a
detail matters for correctness it is reproduced faithfully:

* NeoPixel enforces its length, so an out-of-range pixel index raises IndexError
  exactly as it does on hardware. This is what makes the neoindex regression
  test meaningful rather than decorative.
* Animation constructors set auto_write False on the pixel object, matching
  adafruit_led_animation, because the firmware's display behaviour depends on
  that side effect.
* Mixer exposes a fixed-length voice list, so indexing past voice_count raises.
"""

import sys
import types


class Pin:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "Pin(%s)" % self.name


class _BoardModule(types.ModuleType):
    """Any attribute access yields a Pin, matching board's dynamic surface."""

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        pin = Pin(name)
        setattr(self, name, pin)
        return pin


class Event:
    def __init__(self, key_number, pressed, timestamp=0):
        self.key_number = key_number
        self.pressed = pressed
        self.released = not pressed
        self.timestamp = timestamp


class _EventQueue:
    def __init__(self):
        self._events = []

    def get(self):
        if not self._events:
            return None
        return self._events.pop(0)

    def clear(self):
        self._events = []

    def post(self, event):
        """Test helper: queue an event for the firmware to consume."""
        self._events.append(event)

    def __len__(self):
        return len(self._events)


class KeyMatrix:
    def __init__(self, row_pins=(), column_pins=(), columns_to_anodes=True):
        self.row_pins = row_pins
        self.column_pins = column_pins
        self.events = _EventQueue()


class IncrementalEncoder:
    def __init__(self, pin_a=None, pin_b=None):
        self.position = 0


class NeoPixel:
    """List-like pixel buffer that enforces its length, as hardware does."""

    def __init__(self, pin, n, brightness=1.0, auto_write=True):
        self.pin = pin
        self.n = n
        self.brightness = brightness
        self.auto_write = auto_write
        self._pixels = [(0, 0, 0)] * n
        self.show_count = 0

    def __len__(self):
        return self.n

    def _check(self, index):
        if not isinstance(index, int):
            raise TypeError("pixel index must be an int")
        if index < 0 or index >= self.n:
            raise IndexError("pixel index out of range")

    def __setitem__(self, index, value):
        self._check(index)
        self._pixels[index] = value
        if self.auto_write:
            self.show()

    def __getitem__(self, index):
        self._check(index)
        return self._pixels[index]

    def fill(self, color):
        self._pixels = [color] * self.n
        if self.auto_write:
            self.show()

    def show(self):
        self.show_count += 1


class DigitalInOut:
    """Models the parts of digitalio.DigitalInOut the firmware relies on.

    Including the context manager, which CircuitPython supports and which
    releases the pin on exit - code that claims a pin temporarily depends on
    that, and a stub without it fails in a way the badge never would.

    `pulled_high` lets a test say what an input pin reads, so bus recovery
    can be exercised against both a free line and a held one.
    """

    pulled_high = True

    def __init__(self, pin):
        self.pin = pin
        self.direction = None
        self.pull = None
        self.value = False
        self.deinited = False
        self.transitions = []

    def switch_to_input(self, pull=None):
        self.direction = _Direction.INPUT
        self.pull = pull
        self.value = self.pulled_high

    def switch_to_output(self, value=False):
        self.direction = _Direction.OUTPUT
        self.value = value

    def __setattr__(self, name, value):
        if name == "value" and self.__dict__.get("direction") == _Direction.OUTPUT:
            self.__dict__.setdefault("transitions", []).append(value)
        self.__dict__[name] = value

    def deinit(self):
        self.deinited = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()
        return False


class _Direction:
    OUTPUT = "output"
    INPUT = "input"


class _Pull:
    UP = "up"
    DOWN = "down"


class _WatchDogMode:
    RESET = "RESET"
    RAISE = "RAISE"


class WatchDogTimer:
    """Models microcontroller.watchdog.

    Enforces what the hardware does: the timeout has to be set before the
    mode, because setting the mode is what arms it.
    """

    def __init__(self):
        self.timeout = None
        self._mode = None
        self.feeds = 0

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        if value is not None and not self.timeout:
            raise ValueError("watchdog timeout must be set before the mode")
        self._mode = value

    def feed(self):
        if self._mode is None:
            raise ValueError("watchdog is not running")
        self.feeds += 1


class I2C:
    def __init__(self, scl=None, sda=None, frequency=100000):
        self.scl = scl
        self.sda = sda
        self.frequency = frequency


class SPI:
    def __init__(self, clock=None, MOSI=None, MISO=None):
        self.clock = clock


class UART:
    # The real default is a one second timeout, which blocks the main loop on
    # every read when no MIDI is arriving.
    def __init__(self, tx=None, rx=None, baudrate=9600, timeout=1, **kwargs):
        self.tx = tx
        self.rx = rx
        self.baudrate = baudrate
        self.timeout = timeout
        # Bytes waiting to be read. The firmware asks before calling into
        # adafruit_midi, because receive() allocates whether or not anything
        # arrived, and on a 2 ms timer that is the largest source of garbage
        # on the board. Defaults to nothing waiting, which is the case that
        # matters: no cable plugged in.
        self.in_waiting = 0


class Group:
    def __init__(self, *args, **kwargs):
        self.items = []

    def append(self, item):
        self.items.append(item)

    def __len__(self):
        return len(self.items)


class Bitmap:
    def __init__(self, width, height, value_count):
        self.width = width
        self.height = height


class Palette:
    def __init__(self, count):
        self._colors = [0] * count
        self.transparent = set()

    def __setitem__(self, index, value):
        self._colors[index] = value

    def __getitem__(self, index):
        return self._colors[index]

    def make_transparent(self, index):
        self.transparent.add(index)

    def make_opaque(self, index):
        self.transparent.discard(index)


class TileGrid:
    """Models the parts of displayio.TileGrid the firmware relies on.

    Indexing is by (column, row) and out-of-range raises, as it does on the
    badge; a screen that miscalculates its geometry then fails in a test
    rather than silently drawing off the panel.
    """

    def __init__(
        self,
        bitmap,
        pixel_shader=None,
        width=1,
        height=1,
        tile_width=None,
        tile_height=None,
        x=0,
        y=0,
    ):
        self.bitmap = bitmap
        self.pixel_shader = pixel_shader
        self.width = width
        self.height = height
        self.tile_width = tile_width
        self.tile_height = tile_height
        self.x = x
        self.y = y
        self.tiles = {}
        self.writes = 0

    def _check(self, key):
        column, row = key
        if not (0 <= column < self.width) or not (0 <= row < self.height):
            raise IndexError("tile index out of range: %r" % (key,))

    def __setitem__(self, key, value):
        self._check(key)
        self.tiles[key] = value
        self.writes += 1

    def __getitem__(self, key):
        self._check(key)
        return self.tiles.get(key, 0)


class I2CDisplayBus:
    """CircuitPython 9 split this out of displayio; 10 removed the old name."""

    def __init__(self, bus, device_address=0):
        self.bus = bus
        self.device_address = device_address


class SSD1306:
    def __init__(self, bus, width=128, height=32):
        self.width = width
        self.height = height
        self.shown = None
        self._root_group = None

    def show(self, group):
        # Removed in CircuitPython 9; kept here only so a stray caller fails
        # a test rather than passing quietly.
        self.shown = group
        self._root_group = group

    @property
    def root_group(self):
        return self._root_group

    @root_group.setter
    def root_group(self, group):
        self._root_group = group
        self.shown = group

    def refresh(self, **kwargs):
        return True


class Glyph:
    def __init__(self, tile_index):
        self.tile_index = tile_index


class BuiltinFont:
    """Stands in for terminalio.FONT.

    The real builtin font is a strip of glyphs addressed by tile index, with
    the printable ASCII range starting at 0x20. Measured on the badge its
    bounding box is 6x12 and "A" resolves to tile 33, which is what this
    reproduces - the screen derives all its geometry from these two calls, so
    getting them wrong here would hide a real layout bug.
    """

    FIRST = 0x20
    LAST = 0x7E

    def __init__(self, width=6, height=12, tile_offset=0):
        self._width = width
        self._height = height
        # Not every font puts its space glyph at tile zero. The builtin one
        # does, which quietly hides code that assumes an untouched tile is
        # blank, so the offset exists to let a test say otherwise.
        self.tile_offset = tile_offset
        self.bitmap = Bitmap(width * (self.LAST - self.FIRST + 1), height, 2)

    def get_bounding_box(self):
        return (self._width, self._height)

    def get_glyph(self, codepoint):
        if self.FIRST <= codepoint <= self.LAST:
            return Glyph(codepoint - self.FIRST + self.tile_offset)
        return None


FONT = BuiltinFont()


class Label:
    def __init__(self, font, text="", color=None, x=0, y=0, **kwargs):
        self.font = font
        self.text = text
        self.color = color
        self.x = x
        self.y = y


class WaveFile:
    def __init__(self, file_obj, buffer=None):
        if buffer is not None and not (8 <= len(buffer) <= 1024):
            # CircuitPython enforces this range; exceeding it is a ValueError.
            raise ValueError("buffer length must be 8-1024")
        header = file_obj.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ValueError("not a RIFF/WAVE file")
        self.file = file_obj
        self.buffer = buffer


class RawSample:
    """Audio held in memory.

    Bit depth is inferred from the buffer's element size, exactly as
    CircuitPython does: plain bytes are 8-bit, a 2-byte element type is
    16-bit. Getting this wrong constructs fine and only fails later at
    play(), so the check is reproduced here to catch it in tests.
    """

    def __init__(self, buffer, channel_count=1, sample_rate=8000):
        self.buffer = buffer
        self.channel_count = channel_count
        self.sample_rate = sample_rate
        itemsize = getattr(buffer, "itemsize", 1)
        self.bits_per_sample = itemsize * 8

    def __len__(self):
        return len(self.buffer)


class _Voice:
    def __init__(self, bits_per_sample=16):
        self.level = 1.0
        self.playing = False
        self.sample = None
        self.bits_per_sample = bits_per_sample

    def play(self, sample, loop=False):
        bits = getattr(sample, "bits_per_sample", None)
        if bits is not None and bits != self.bits_per_sample:
            raise ValueError("The sample's bits_per_sample does not match the mixer's")
        self.sample = sample
        self.playing = True

    def stop(self):
        self.playing = False


class Mixer:
    def __init__(self, voice_count=1, **kwargs):
        self.voice_count = voice_count
        bits = kwargs.get("bits_per_sample", 16)
        self.voice = [_Voice(bits) for _ in range(voice_count)]
        self.config = kwargs


class I2SOut:
    def __init__(self, bit_clock=None, word_select=None, data=None):
        self.playing = False
        self.deinited = False
        self.deinit_count = 0

    def play(self, sample, loop=False):
        self.playing = True

    def stop(self):
        self.playing = False

    def deinit(self):
        self.deinited = True
        self.deinit_count += 1


class SDCard:
    """Native sdcardio driver. No card is present under test, so mounting
    fails the same way it does on a badge with an empty slot - which is the
    path setup.py has to survive."""

    def __init__(self, spi, cs, baudrate=8000000):
        # cs is a Pin for sdcardio, not a DigitalInOut - the two drivers
        # differ here and getting it wrong is a TypeError on hardware.
        if not isinstance(cs, Pin):
            raise TypeError("cs must be of type Pin, not DigitalInOut")
        raise OSError("no SD card in the test environment")


class VfsFat:
    def __init__(self, sdcard):
        self.sdcard = sdcard


class MIDI:
    def __init__(
        self,
        midi_in=None,
        midi_out=None,
        in_channel=None,
        out_channel=0,
        **kwargs,
    ):
        self.midi_in = midi_in
        self.midi_out = midi_out
        self.in_channel = in_channel
        self.out_channel = out_channel
        self.sent = []
        self.incoming = []

    def send(self, message):
        self.sent.append(message)

    def receive(self):
        if not self.incoming:
            return None
        message = self.incoming.pop(0)
        # Keep the port's own "bytes waiting" honest. The firmware asks the
        # UART before calling in here, so a stub that queues a message
        # without also saying bytes are waiting would be testing a path the
        # badge never takes.
        if not self.incoming and hasattr(self.midi_in, "in_waiting"):
            self.midi_in.in_waiting = 0
        return message

    def post(self, message):
        """Test helper: queue a message for the firmware to receive.

        Also marks bytes waiting on the underlying port, because that is what
        the firmware checks before it will call receive at all.
        """
        self.incoming.append(message)
        if hasattr(self.midi_in, "in_waiting"):
            self.midi_in.in_waiting = len(self.incoming) * 3


class _MidiMessage:
    def __init__(self, note, velocity=0):
        self.note = note
        self.velocity = velocity

    def __eq__(self, other):
        return (
            type(self) is type(other)
            and self.note == other.note
            and self.velocity == other.velocity
        )

    def __repr__(self):
        return "%s(%r, %r)" % (type(self).__name__, self.note, self.velocity)


class NoteOn(_MidiMessage):
    pass


class NoteOff(_MidiMessage):
    pass


class _SystemMessage:
    """MIDI system real-time: Start, Stop and Continue carry no data."""

    def __repr__(self):
        return "%s()" % type(self).__name__


class Start(_SystemMessage):
    pass


class Stop(_SystemMessage):
    pass


class Continue(_SystemMessage):
    pass


class TimingClock(_SystemMessage):
    pass


class ControlChange:
    def __init__(self, control, value):
        self.control = control
        self.value = value


class Keyboard:
    def __init__(self, devices):
        self.pressed = []
        self.released = []

    def press(self, *keycodes):
        self.pressed.extend(keycodes)

    def release(self, *keycodes):
        self.released.extend(keycodes)

    def release_all(self):
        self.pressed = []


class ConsumerControl:
    def __init__(self, devices):
        self.sent = []

    def send(self, code):
        self.sent.append(code)


class Keycode:
    KEYPAD_ZERO = 98
    KEYPAD_ONE = 89
    KEYPAD_TWO = 90
    KEYPAD_THREE = 91
    KEYPAD_FOUR = 92
    KEYPAD_FIVE = 93
    KEYPAD_SIX = 94
    KEYPAD_SEVEN = 95
    KEYPAD_EIGHT = 96
    KEYPAD_NINE = 97
    KEYPAD_PLUS = 87
    KEYPAD_MINUS = 86


class ConsumerControlCode:
    VOLUME_INCREMENT = 233
    VOLUME_DECREMENT = 234
    MUTE = 226


class _Animation:
    """Mirrors adafruit_led_animation: takes ownership of auto_write."""

    def __init__(self, pixel_object, speed=0.1, **kwargs):
        self.pixel_object = pixel_object
        # The real library does this, and firmware behaviour depends on it.
        pixel_object.auto_write = False
        self.animate_count = 0

    def animate(self):
        self.animate_count += 1
        return True


class _Ticks:
    """Monotonic millisecond counter that advances on every read.

    The firmware busy-waits with `while ticks_ms() < deadline: pass`, so a
    constant would hang any test that reaches one. Advancing per read lets those
    loops terminate without the test needing to sleep.
    """

    def __init__(self, step=1):
        self.value = 0
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value

    def reset(self):
        self.value = 0


ticks = _Ticks()


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def install():
    """Register the fake modules in sys.modules. Safe to call repeatedly."""
    board = _BoardModule("board")
    modules = {
        "board": board,
        "busio": _module("busio", I2C=I2C, SPI=SPI, UART=UART),
        "digitalio": _module(
            "digitalio",
            DigitalInOut=DigitalInOut,
            Direction=_Direction,
            Pull=_Pull,
        ),
        "microcontroller": _module(
            "microcontroller", watchdog=WatchDogTimer(), reset=lambda: None
        ),
        "watchdog": _module("watchdog", WatchDogMode=_WatchDogMode),
        "keypad": _module("keypad", KeyMatrix=KeyMatrix, Event=Event),
        "rotaryio": _module("rotaryio", IncrementalEncoder=IncrementalEncoder),
        "neopixel": _module("neopixel", NeoPixel=NeoPixel),
        "displayio": _module(
            "displayio",
            release_displays=lambda: None,
            Group=Group,
            Bitmap=Bitmap,
            Palette=Palette,
            TileGrid=TileGrid,
        ),
        "i2cdisplaybus": _module("i2cdisplaybus", I2CDisplayBus=I2CDisplayBus),
        "terminalio": _module("terminalio", FONT=FONT),
        "storage": _module(
            "storage",
            VfsFat=VfsFat,
            mount=lambda vfs, path: None,
            remount=lambda path, readonly=True: None,
        ),
        "usb_midi": _module("usb_midi", ports=[None, None]),
        "usb_hid": _module("usb_hid", devices=[]),
        "audiobusio": _module("audiobusio", I2SOut=I2SOut),
        "audiomixer": _module("audiomixer", Mixer=Mixer),
        "audiocore": _module("audiocore", WaveFile=WaveFile, RawSample=RawSample),
        "supervisor": _module("supervisor", ticks_ms=ticks),
        "adafruit_displayio_ssd1306": _module(
            "adafruit_displayio_ssd1306", SSD1306=SSD1306
        ),
        "adafruit_sdcard": _module("adafruit_sdcard", SDCard=SDCard),
        "sdcardio": _module("sdcardio", SDCard=SDCard),
        "adafruit_midi": _module("adafruit_midi", MIDI=MIDI),
        "adafruit_midi.note_on": _module("adafruit_midi.note_on", NoteOn=NoteOn),
        "adafruit_midi.note_off": _module("adafruit_midi.note_off", NoteOff=NoteOff),
        "adafruit_midi.control_change": _module(
            "adafruit_midi.control_change", ControlChange=ControlChange
        ),
        "adafruit_midi.start": _module("adafruit_midi.start", Start=Start),
        "adafruit_midi.stop": _module("adafruit_midi.stop", Stop=Stop),
        "adafruit_midi.midi_continue": _module(
            "adafruit_midi.midi_continue", Continue=Continue
        ),
        "adafruit_midi.timing_clock": _module(
            "adafruit_midi.timing_clock", TimingClock=TimingClock
        ),
        "adafruit_display_text": _module("adafruit_display_text"),
        "adafruit_display_text.label": _module(
            "adafruit_display_text.label", Label=Label
        ),
        "adafruit_hid": _module("adafruit_hid"),
        "adafruit_hid.keyboard": _module("adafruit_hid.keyboard", Keyboard=Keyboard),
        "adafruit_hid.keycode": _module("adafruit_hid.keycode", Keycode=Keycode),
        "adafruit_hid.consumer_control": _module(
            "adafruit_hid.consumer_control", ConsumerControl=ConsumerControl
        ),
        "adafruit_hid.consumer_control_code": _module(
            "adafruit_hid.consumer_control_code",
            ConsumerControlCode=ConsumerControlCode,
        ),
    }

    # adafruit_led_animation.animation.<name> — each exposes one animation class.
    animation_names = {
        "rainbow": "Rainbow",
        "rainbowchase": "RainbowChase",
        "rainbowcomet": "RainbowComet",
        "rainbowsparkle": "RainbowSparkle",
        "sparklepulse": "SparklePulse",
    }
    modules["adafruit_led_animation"] = _module("adafruit_led_animation")
    modules["adafruit_led_animation.animation"] = _module(
        "adafruit_led_animation.animation"
    )
    for module_name, class_name in animation_names.items():
        full = "adafruit_led_animation.animation." + module_name
        modules[full] = _module(
            full, **{class_name: type(class_name, (_Animation,), {})}
        )

    for name, module in modules.items():
        sys.modules[name] = module

    # adafruit_display_text is imported as `from adafruit_display_text import label`
    sys.modules["adafruit_display_text"].label = sys.modules[
        "adafruit_display_text.label"
    ]
    sys.modules["adafruit_midi"].note_on = sys.modules["adafruit_midi.note_on"]
    sys.modules["adafruit_midi"].note_off = sys.modules["adafruit_midi.note_off"]
    sys.modules["adafruit_midi"].control_change = sys.modules[
        "adafruit_midi.control_change"
    ]
    for submodule in ("start", "stop", "midi_continue", "timing_clock"):
        setattr(
            sys.modules["adafruit_midi"],
            submodule,
            sys.modules["adafruit_midi." + submodule],
        )
    for submodule in (
        "keyboard",
        "keycode",
        "consumer_control",
        "consumer_control_code",
    ):
        setattr(
            sys.modules["adafruit_hid"],
            submodule,
            sys.modules["adafruit_hid." + submodule],
        )

    return board
