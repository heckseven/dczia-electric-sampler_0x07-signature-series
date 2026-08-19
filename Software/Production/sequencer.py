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
from audiocore import RawSample, WaveFile
from supervisor import ticks_ms

from engine import wav

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

# The mixer has one fixed format and every sample must match it. The rate is a
# trade rather than a hardware limit: lower costs bandwidth but leaves more
# room for voices and for holding samples in RAM. Measured cost per playing
# voice - 31.25 KB/s here, 43.1 at 22050, 86.2 at 44100 - against a card that
# streams about 437 KB/s and a RAM budget below.
#
# The original firmware wandered over this ground already: 22050 at first,
# dropped to 16000 in the same commit that took the sampler from one voice to
# nine, then back to 22050 for the production rewrite. Neither move recorded a
# measurement. Tools/convert_samples.py --rate converts material to match.
SAMPLE_RATE = 16000
CHANNELS = 1
BITS = 16

# Samples are loaded into RAM rather than streamed wherever they fit, because
# storage cannot reliably feed the mixer. Measured on this board: audio needs
# 43.1 KB/s per playing voice, flash sustains 391 KB/s and the SD card only
# 169 KB/s in the small reads streaming produces. Three voices from a card is
# already marginal and eight tracks would need 345 KB/s, which starves the I2S
# buffer and sounds like harsh digital distortion.
#
# The budget is deliberately conservative against the ~86 KB free measured with
# the engine loaded. A sample too big for it still plays, by streaming, which is
# fine for the one long sound in a kit and bad only if every track is long.
RAM_BUDGET = 48 * 1024
MAX_RAM_SAMPLE = 24 * 1024

# Read buffer for tracks that are too big for RAM and must stream. CircuitPython
# caps WaveFile's buffer at 1024 bytes - larger raises "buffer length must be
# 8-1024" - and the cap costs real throughput: the card sustains 679 KB/s in
# 4 KB reads but only 333 KB/s in 1 KB ones. 1024 is therefore simply the most
# the runtime allows, and streaming capacity is set by that, not by the card.
STREAM_BUFFER = 1024

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

# How often the MIDI ports are drained. Reading them costs about 430 us a pass
# on this board, mostly USB, against a main loop that is otherwise around
# 200 us - so polling every iteration triples the loop period and thins the
# margin for catching a sync pulse by polling. MIDI messages are rare and a
# couple of milliseconds of latency on a transport command is inaudible, so
# the ports are drained on a timer instead. Sync input, which genuinely needs
# every pass, is untouched.
MIDI_POLL_MS = 2


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


def sample_candidates(name, lister=None, dirs=SAMPLE_DIRS):
    """Every path a bare filename could refer to, nearest store first.

    More than one is returned deliberately. A copy on the card shadows one in
    flash by name, but the shadowing copy may be unusable - left over from a
    different mixer rate, say - and the badge should fall through to a copy it
    can actually play rather than reporting the track as silent.
    """
    if name.startswith("/"):
        return [name]
    found = []
    for directory in dirs:
        try:
            names = lister(directory) if lister else _listdir(directory)
        except OSError:
            continue
        if name in names:
            found.append(directory + "/" + name)
    return found


def _listdir(directory):
    from os import listdir

    return listdir(directory)


def resolve_sample(name, lister=None, dirs=SAMPLE_DIRS):
    """The first path a bare filename refers to, or None."""
    candidates = sample_candidates(name, lister, dirs)
    return candidates[0] if candidates else None


class Sequencer:
    def __init__(self):
        self.song = Song()
        self.clock = Clock()
        self.transport = Transport()
        self.strength = quantize.DEFAULT_STRENGTH
        self.mode = LIVE
        self.selected_track = 0
        self.page = 0
        # Two voices per track, alternating, so a retrigger never cuts a
        # sample that is still ringing. Cutting one mid-waveform is a
        # full-scale discontinuity and it clicks audibly - confirmed by ear on
        # the badge, where the same pattern was clean with two voices and
        # clicked with one.
        #
        # A fade would be the textbook alternative, and it is not available
        # here: MixerVoice exposes only `level`, a level change lands on a
        # buffer boundary, and the smallest buffer is 8 ms against the 1-3 ms
        # such declicks need. Tested at 8 and 16 ms and both still clicked.
        #
        # Two is enough. Going to three or four made no audible difference,
        # even though a voice is reused before a long sample has finished, and
        # voices are nearly free - 48 of them cost 1328 bytes.
        self.poly = True

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
        self._streamed = [False] * TRACK_COUNT
        self._sizes = [0] * TRACK_COUNT
        self._ram_used = 0
        self._next_voice = [0] * TRACK_COUNT
        self.midi_out = [False] * TRACK_COUNT  # per track, opt in

        self._sync_out_until = None
        self._sync_in_high = True
        self._last_step = None
        self._last_midi_poll = 0
        self.last_error = None

    # --- kit --------------------------------------------------------------

    def load_track(self, track, path):
        """Point a track at a sample. A failure leaves the track silent.

        A bare filename is resolved across the sample directories, so a kit
        saved when samples lived in flash still loads once they are on the
        card. Every candidate is tried, not just the first: a stale copy on the
        card shadows flash by name, and falling through to a playable copy is
        better than reporting a track silent when a usable sample is present.
        """
        self._release_track(track)
        if not path:
            return False
        for candidate in sample_candidates(path) or [path]:
            if self._load_one(track, candidate):
                return True
        return False

    def _load_one(self, track, path):
        try:
            handle = open(path, "rb")
        except OSError:
            return False

        try:
            rate, channels, bits, offset, size = wav.read_format(handle)
        except (OSError, wav.WavError):
            handle.close()
            return False

        if not wav.matches(rate, channels, bits, SAMPLE_RATE, CHANNELS, BITS):
            # The mixer has one fixed format; anything else plays at the wrong
            # pitch or not at all. Refuse it rather than make a mess of it.
            handle.close()
            self.last_error = "%s is %s, need %s" % (
                path,
                wav.describe(rate, channels, bits),
                wav.describe(SAMPLE_RATE, CHANNELS, BITS),
            )
            return False

        sample = self._load_to_ram(handle, offset, size)
        if sample is not None:
            handle.close()
            self._samples[track] = sample
            self._streamed[track] = False
            self._sizes[track] = size
        else:
            try:
                handle.seek(0)
                self._samples[track] = WaveFile(handle, bytearray(STREAM_BUFFER))
            except (OSError, ValueError):
                handle.close()
                return False
            self._files[track] = handle
            self._streamed[track] = True

        self.song.set_sample(track, path)
        return True

    def _load_to_ram(self, handle, offset, size):
        """Read the audio into memory, or return None to fall back to streaming."""
        if size > MAX_RAM_SAMPLE or self._ram_used + size > RAM_BUDGET:
            return None
        try:
            handle.seek(offset)
            data = handle.read(size)
        except (OSError, MemoryError):
            return None
        if len(data) < size:
            return None
        try:
            # RawSample infers bit depth from the buffer's element size: raw
            # bytes mean 8-bit, which the mixer rejects at play() with "the
            # sample's bits_per_sample does not match". Casting to 16-bit
            # signed says what the audio actually is. A memoryview rather than
            # array.array('h', data) so the audio is not copied - a second
            # copy of every sample would double peak memory during loading.
            sample = RawSample(
                memoryview(data).cast("h"),
                channel_count=CHANNELS,
                sample_rate=SAMPLE_RATE,
            )
        except (ValueError, MemoryError):
            return None
        self._ram_used += size
        return sample

    def is_streamed(self, track):
        """True when a track plays from storage rather than RAM."""
        return self._streamed[track]

    @property
    def ram_used(self):
        return self._ram_used

    def load_kit(self, paths):
        loaded = 0
        for track in range(TRACK_COUNT):
            path = paths[track] if track < len(paths) else None
            if self.load_track(track, path):
                loaded += 1
        return loaded

    def _release_track(self, track):
        if self._samples[track] is not None and not self._streamed[track]:
            # Reclaim the budget this track's audio was holding.
            self._ram_used = max(0, self._ram_used - self._sample_bytes(track))
        self._sizes[track] = 0
        self._streamed[track] = False
        self._samples[track] = None
        handle = self._files[track]
        self._files[track] = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _sample_bytes(self, track):
        return self._sizes[track]

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
        # Sync in is polled every pass: an edge lasts only milliseconds and
        # missing one loses the beat. MIDI can wait a moment.
        self._poll_sync_in(now)
        if ticks_diff(now, self._last_midi_poll) >= MIDI_POLL_MS:
            self._last_midi_poll = now
            self.poll_midi_in()
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
