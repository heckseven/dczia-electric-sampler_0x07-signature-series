"""The sequencer engine: the piece that owns the beat.

This is a module-level singleton, not a State. The old design made the
sequencer a state in the menu, so leaving it to change an LED animation tore
down the audio and lost the pattern. Here the engine is ticked from the main
loop regardless of what is on screen, which is what lets the beat keep playing
while you browse samples, adjust lighting or sit in the menu. States become
pure UI: they render and handle input, and ask the engine for what they need.

Audio is allocated once, here, and never torn down. The previous sequencer
built an I2SOut on entry and released it (sometimes) on exit, which leaked the
peripheral and raised RuntimeError on re-entry.

Everything with real logic in it lives in engine/, which imports nothing from
CircuitPython and is tested directly. This module is the thin layer that binds
that logic to the mixer, the MIDI ports, the LEDs and the sync jacks.
"""

import audiobusio
import audiomixer
import board
from adafruit_midi.midi_continue import Continue
from adafruit_midi.note_on import NoteOn
from adafruit_midi.start import Start
from adafruit_midi.stop import Stop
from audiocore import WaveFile
from supervisor import ticks_ms

from engine import quantize
from engine.clock import Clock, ticks_diff
from engine.song import DEFAULT_VELOCITY, TRACK_COUNT, Song
from engine.transport import LIVE, SEQ, Transport
from setup import midi_serial, midi_usb, sync_in, sync_out

# Where samples are looked for, in order. The SD card comes first: it is
# where kits and patterns live, it is the only writable store (CIRCUITPY is
# read-only to the badge while USB is attached), and moving samples there is
# what frees room on a 490 KB volume for the firmware itself.
SAMPLE_DIRS = ("/sd/samples", "/samples")

SAMPLE_RATE = 22050
CHANNELS = 1
BITS = 16

# Two voices per track plus two spare. Mono per track is the default, but
# voices are nearly free - 24 of them measured at about 1.1KB against 141KB
# free - so the mixer is built with room for the polyphonic mode from the
# start. Switching mode then only changes which voice a hit is routed to, and
# never has to rebuild the mixer underneath a playing pattern.
VOICES_PER_TRACK = 2
AUDITION_VOICE = TRACK_COUNT * VOICES_PER_TRACK
METRONOME_VOICE = AUDITION_VOICE + 1
MIXER_VOICES = METRONOME_VOICE + 1

# Sync output pulse width. Long enough for anything downstream to see the
# edge, far shorter than a step at any usable tempo.
SYNC_PULSE_MS = 5


def list_samples(lister=None, dirs=SAMPLE_DIRS):
    """Every .wav across the sample directories, as (name, full path).

    Earlier directories win, so a sample on the SD card shadows one of the
    same name in flash. A directory that does not exist is simply skipped -
    a badge with no card is not an error.
    """
    if lister is None:
        from os import listdir as lister
    found = []
    seen = set()
    for directory in dirs:
        try:
            names = lister(directory)
        except OSError:
            continue
        for name in sorted(names):
            if not name.endswith(".wav") or name.startswith("."):
                continue
            if name in seen:
                continue
            seen.add(name)
            found.append((name, directory + "/" + name))
    return found


def resolve_sample(name, lister=None, dirs=SAMPLE_DIRS):
    """Find a sample by bare filename, so a kit survives moving between stores."""
    if name.startswith("/"):
        return name
    for found_name, path in list_samples(lister, dirs):
        if found_name == name:
            return path
    return None


class Sequencer:
    def __init__(self):
        self.song = Song()
        self.clock = Clock()
        self.transport = Transport()
        self.strength = quantize.DEFAULT_STRENGTH
        self.mode = LIVE
        self.selected_track = 0
        self.page = 0
        self.poly = False  # one voice per track; retrigger cuts, as a 909 does

        # Whether pulses arriving on the sync jack should start a stopped
        # transport, or only set tempo and phase for a transport the player
        # started. Off by default: a stray clock on a busy patch should not
        # decide the badge is playing. There is no way to do better
        # automatically - the sync jack's switch contacts (J4 pads 1-3) are
        # unconnected on this board, so cable insertion cannot be detected.
        self.sync_starts_transport = False

        self.audio = audiobusio.I2SOut(board.GP0, board.GP1, board.GP2)
        self.mixer = audiomixer.Mixer(
            voice_count=MIXER_VOICES,
            sample_rate=SAMPLE_RATE,
            channel_count=CHANNELS,
            bits_per_sample=BITS,
            samples_signed=True,
        )
        self.audio.play(self.mixer)

        self._samples = [None] * TRACK_COUNT
        self._files = [None] * TRACK_COUNT
        self._next_voice = [0] * TRACK_COUNT
        self.midi_out = [False] * TRACK_COUNT  # per track, opt in

        self._sync_out_until = None
        self._sync_in_high = True
        self._last_step = None

    # --- kit --------------------------------------------------------------

    def load_track(self, track, path):
        """Point a track at a sample. A failure leaves the track silent.

        A bare filename is resolved across the sample directories, so a kit
        saved when samples lived in flash still loads once they are on the
        card.
        """
        self._release_track(track)
        if not path:
            return False
        path = resolve_sample(path) or path
        try:
            handle = open(path, "rb")
        except OSError:
            return False
        try:
            self._samples[track] = WaveFile(handle)
        except (OSError, ValueError):
            handle.close()
            return False
        self._files[track] = handle
        self.song.set_sample(track, path)
        return True

    def load_kit(self, paths):
        loaded = 0
        for track in range(TRACK_COUNT):
            path = paths[track] if track < len(paths) else None
            if self.load_track(track, path):
                loaded += 1
        return loaded

    def _release_track(self, track):
        self._samples[track] = None
        handle = self._files[track]
        self._files[track] = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def has_sample(self, track):
        return self._samples[track] is not None

    # --- voices -----------------------------------------------------------

    def _voice_for(self, track):
        """Which mixer voice this track's next hit should use.

        Mono, the default, always returns the same voice so a retrigger cuts
        the previous hit. Polyphonic alternates between two, so the previous
        hit decays naturally instead of being cut - which is the only way to
        avoid a retrigger click here, since a fade would need sub-millisecond
        ramping that interpreted code cannot do.
        """
        base = track * VOICES_PER_TRACK
        if not self.poly:
            return base
        self._next_voice[track] ^= 1
        return base + self._next_voice[track]

    def trigger(self, track, velocity):
        """Sound one hit. Used by both the sequencer and live pads."""
        sample = self._samples[track]
        if sample is None:
            return False
        voice = self.mixer.voice[self._voice_for(track)]
        voice.level = velocity / 127.0
        voice.play(sample)
        if self.midi_out[track]:
            self._send_midi(track, velocity)
        return True

    def audition(self, path):
        """Preview a sample without disturbing any track."""
        try:
            handle = open(path, "rb")
            sample = WaveFile(handle)
        except (OSError, ValueError):
            return False
        voice = self.mixer.voice[AUDITION_VOICE]
        voice.level = 0.5
        voice.play(sample)
        return True

    def _send_midi(self, track, velocity):
        note = 36 + track  # General MIDI drum range, kick upward
        message = NoteOn(note, velocity)
        midi_serial.send(message)
        midi_usb.send(message)

    # --- transport --------------------------------------------------------

    def toggle_play(self):
        now = ticks_ms()
        if self.transport.toggle_play():
            self.clock.reset()
            self.clock.start(now)
            self._last_step = None
        else:
            self.clock.stop()
        return self.transport.playing

    def toggle_record(self):
        return self.transport.toggle_record()

    def pad_hit(self, track):
        """A pad was struck. Sounds it, may start the take, may record it."""
        started = self.transport.pad_hit(self.mode)
        if started:
            now = ticks_ms()
            self.clock.reset()
            self.clock.start(now)
            self._last_step = None
        self.trigger(track, DEFAULT_VELOCITY)
        if self.transport.should_capture(self.mode):
            self.capture(track)
        return started

    def capture(self, track):
        """Write a live hit into the pattern at the current position."""
        step, offset = quantize.quantize_hit(
            self.clock.tick, self.song.ticks_per_step, self.song.length
        )
        self.song.set_step(track, step, DEFAULT_VELOCITY, offset)
        return step

    def erase(self, track):
        """Live erase: clear this track's hit at the current position."""
        step = quantize.nearest_step(
            self.clock.tick % (self.song.length * self.song.ticks_per_step),
            self.song.ticks_per_step,
            self.song.length,
        )
        self.song.clear_step(track, step)
        return step

    # --- the poll ---------------------------------------------------------

    def tick(self):
        """Call once per main loop pass. Never blocks."""
        now = ticks_ms()
        self.poll_midi_in()
        self._poll_sync_in(now)
        self._update_sync_out(now)

        fired = self.clock.update(now)
        for _ in range(fired):
            self._on_tick(now)

    def _on_tick(self, now):
        tick = self.clock.tick
        for track, _step, velocity in quantize.hits_due(self.song, tick, self.strength):
            self.trigger(track, velocity)
        if self.clock.sync_out_due(tick):
            self._pulse_sync_out(now)

    @property
    def current_step(self):
        total = self.song.length * self.song.ticks_per_step
        return (self.clock.tick % total) // self.song.ticks_per_step

    # --- sync -------------------------------------------------------------

    def poll_midi_in(self):
        """Act on MIDI transport messages from either port.

        Start, Stop and Continue are the standard way a sequencer is driven
        remotely, and they give an explicit play intent that an anonymous
        stream of sync pulses cannot: the badge can stay out of the way of a
        clock it is merely listening to, while still obeying a real Start.
        """
        for port in (midi_serial, midi_usb):
            message = port.receive()
            if message is None:
                continue
            if isinstance(message, Start):
                self._remote_start(reset=True)
            elif isinstance(message, Continue):
                self._remote_start(reset=False)
            elif isinstance(message, Stop):
                if self.transport.playing:
                    self.transport.stop()
                    self.clock.stop()

    def _remote_start(self, reset):
        if self.transport.playing:
            return
        now = ticks_ms()
        self.transport.start()
        if reset:
            self.clock.reset()
        self.clock.start(now)
        self._last_step = None

    def _poll_sync_in(self, now):
        """Detect a falling edge on the sync input.

        GP6 idles high and is pulled low by Q1 for each incoming pulse, so a
        sync pulse is a falling edge. countio would catch these in hardware but
        cannot be used: on RP2040 it needs a PWM channel B pin and GP6 is
        channel A. Polling is sound for the 5-15 ms pulses this kind of gear
        emits, provided the main loop stays well under that - which is exactly
        what removing the blocking per-step wait bought us.
        """
        high = sync_in.value
        if self._sync_in_high and not high:
            self.clock.external_pulse(now)
            if not self.transport.playing and self.sync_starts_transport:
                self.transport.start()
                self.clock.start(now)
        self._sync_in_high = high

    def _pulse_sync_out(self, now):
        sync_out.value = True
        self._sync_out_until = now

    def _update_sync_out(self, now):
        if self._sync_out_until is None:
            return
        if ticks_diff(now, self._sync_out_until) >= SYNC_PULSE_MS:
            sync_out.value = False
            self._sync_out_until = None

    # --- settings ---------------------------------------------------------

    def set_strength(self, value):
        """The global quantise strength. Tracks may override it individually."""
        self.strength = quantize.clamp_strength(value)
        return self.strength

    def set_track_strength(self, track, value):
        return self.song.set_track_strength(track, value)

    def strength_for(self, track):
        return self.song.strength_for(track, self.strength)

    def nudge_strength(self, direction):
        return self.set_strength(self.strength + direction * quantize.STRENGTH_STEP)

    def set_bpm(self, value):
        return self.clock.set_bpm(value)

    def select_track(self, track):
        self.selected_track = track % TRACK_COUNT
        return self.selected_track

    def toggle_mode(self):
        self.mode = SEQ if self.mode == LIVE else LIVE
        return self.mode

    def set_page(self, page):
        self.page = page % max(1, self.song.page_count)
        return self.page


# The singleton. Importing this module allocates the audio path once, for the
# lifetime of the program.
engine = Sequencer()
