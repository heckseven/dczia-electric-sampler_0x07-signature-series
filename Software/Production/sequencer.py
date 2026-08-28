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
from adafruit_midi.timing_clock import TimingClock
from audiocore import RawSample, WaveFile
from supervisor import ticks_ms

from engine import wav

from engine import quantize
from engine.util import (
    VOLUME_STEPS,
    accelerated,
    clamp,
    level_for_position,
    position_for_level,
)
from engine.clock import Clock, ticks_diff
from engine.song import DEFAULT_VELOCITY, MAX_VELOCITY, TRACK_COUNT, Song
from engine.transport import LIVE, SEQ, Transport
from setup import midi_serial, midi_uart, midi_usb, sync_in, sync_out

# Where samples are looked for, in order. The SD card comes first: it is
# where kits and patterns live, it is the only writable store (CIRCUITPY is
# read-only to the badge while USB is attached), and moving samples there is
# what frees room on a 490 KB volume for the firmware itself.
SAMPLE_DIRS = ("/sd/samples", "/samples")

# Most samples a listing will return. A card is written by a computer and a
# drum library can hold thousands; turning all of them into a list is work
# and memory the badge has not got. Far above any kit and far below the
# heap. What the browser does with a list this long is its own problem - see
# engine/settings.MAX_ROWS.
MAX_SAMPLES = 512

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
# Loaded at startup so the badge makes a sound out of the box. Bare names, so
# they resolve wherever the samples actually live - card first, then flash.
DEFAULT_KIT = ("Kick.wav", "Snare.wav", "Tom.wav")

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

# How long the I2S stream keeps running after the last sound before it is
# stopped.
#
# The stream is not left running while idle. An active stream makes the
# amplifier sensitive to traffic on the shared supply, and the display is on
# that supply: with the stream running, redrawing the screen pops audibly;
# with it stopped, the identical redrawing is silent. Confirmed on the badge.
# The original firmware only ever created its I2S output on entering the
# sampler, so browsing menus was quiet - this restores that while keeping the
# engine always available.
#
# The linger stops the stream flapping between hits, and means the start pop
# lands under a drum hit rather than in silence.
STREAM_LINGER_MS = 750

# How loud one voice is at full velocity. The mixer sums its voices, so this
# is a polyphony budget rather than a taste setting: four voices at full
# velocity reach full scale together and anything above that clips hard.
#
# It matters because the default beat plays a kick and a snare on the same
# step. At velocity/127 straight through, those two alone summed to 1.65 of
# full scale, which is a loud crunch on the first step where two voices
# coincide - heard on the badge as a few clean hits and then distortion. The
# original firmware ran every voice at 0.1 for the same reason.
DEFAULT_VOLUME = 0.25

# The knob moves the master volume one notch at a time, and the notches are
# spaced evenly in decibels rather than evenly in level - see
# engine.util.level_for_position. A linear scale crowds all the useful
# adjustment into the bottom of the range, which is where headphones live:
# measured on the badge, a twentieth of full level was already loud in a
# pair, and the step either side of it was a doubling.
MIN_VOLUME = 0.0
MAX_VOLUME = 1.0

# How often the MIDI ports are drained. Reading them costs about 430 us a pass
# on this board, mostly USB, against a main loop that is otherwise around
# 200 us - so polling every iteration triples the loop period and thins the
# margin for catching a sync pulse by polling. MIDI messages are rare and a
# couple of milliseconds of latency on a transport command is inaudible, so
# the ports are drained on a timer instead. Sync input, which genuinely needs
# every pass, is untouched.
MIDI_POLL_MS = 2

# What a MIDI clock message is worth. Fixed at 24 a quarter note by the MIDI
# standard, and the same rate this engine ticks at, so one message is one
# tick. Not the same thing as the analog jack's rate, which the player picks.
MIDI_CLOCK_PPQN = 24

# USB MIDI is polled on its own timer because, unlike the serial port, it
# cannot be asked whether anything is waiting: receive() allocates on every
# call regardless. Transport messages are rare enough that 20 ms is
# imperceptible, and it turns 64 bytes every 2 ms into 64 bytes every 20.
#
# A clock over USB is the one thing this interval is not good enough for. The
# ticks come out right - one message is one tick however late it is read - but
# their timing is quantised to this, and 20 ms against a 20.8 ms tick period
# at 120 BPM is audible jitter. A hardware MIDI lead, drained every 2 ms
# above, is the accurate way to be clocked.
USB_MIDI_POLL_MS = 20

# How many messages one poll will take. A clock master sends 24 a quarter
# note, which at 300 BPM is 120 a second, so a poll that took one message
# would drop most of them and the badge would run behind whatever it was
# following. Bounded for the same reason the key queue is: a backlog must not
# be able to spend an unbounded amount of one pass, because the audio buffer
# holds 32 ms and nothing refills it while this runs.
MAX_MIDI_PER_POLL = 8


def list_samples(lister=None, dirs=None, limit=MAX_SAMPLES):
    """Every .wav across the sample directories, as (name, full path).

    Earlier directories win, so a sample on the SD card shadows one of the
    same name in flash. A directory that does not exist is simply skipped -
    a badge with no card is not an error, and neither is a card holding more
    than `limit` samples: the list stops there rather than the badge doing.
    """
    # Read here rather than taken as a default argument: a default is bound
    # when the function is defined, so SAMPLE_DIRS could never afterwards be
    # pointed anywhere else - which is exactly what the tests need to do.
    # `is None` rather than a falsy test, so a caller asking for no
    # directories at all gets no directories rather than all of them.
    dirs = SAMPLE_DIRS if dirs is None else dirs
    if lister is None:
        from os import listdir as lister
    found = []
    seen = set()
    for directory in dirs:
        try:
            names = lister(directory)
        except (OSError, MemoryError):
            # MemoryError as well: a directory with tens of thousands of
            # entries is turned into a list of strings before anything can
            # look at its length, and that is not an error the caller of a
            # sample browser should have to handle.
            continue
        for name in sorted(names):
            if not name.endswith(".wav") or name.startswith("."):
                continue
            if name in seen:
                continue
            seen.add(name)
            found.append((name, directory + "/" + name))
            if len(found) >= limit:
                return found
    return found


def sample_candidates(name, lister=None, dirs=None):
    """Every path a bare filename could refer to, nearest store first.

    More than one is returned deliberately. A copy on the card shadows one in
    flash by name, but the shadowing copy may be unusable - left over from a
    different mixer rate, say - and the badge should fall through to a copy it
    can actually play rather than reporting the track as silent.
    """
    dirs = SAMPLE_DIRS if dirs is None else dirs
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


def resolve_sample(name, lister=None, dirs=None):
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
        # One voice per track. Two alternating voices let a hit ring through
        # its own retrigger, but it also means one sample object is played on
        # two mixer voices at once - harmless for a RAM sample, corrupting for
        # a streamed one, which shares a file position and a read buffer
        # between them. It stays available as a setting rather than a default.
        self.poly = False
        # Scales every voice; see DEFAULT_VOLUME.
        # Held as a knob position so the steps are even to the ear, with
        # the level derived from it.
        self.volume_position = position_for_level(DEFAULT_VOLUME)
        self.volume = level_for_position(self.volume_position)
        # What velocity each mixer voice was last given, so a volume change
        # can be applied to whatever is sounding rather than only to the
        # next hit. Turning the volume down has to be immediate: on
        # headphones the next hit is too late to matter.
        self._voice_velocity = [0] * MIXER_VOICES
        # When the volume knob last moved, so its speed can be read back.
        self._last_volume_turn = None

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
        # Deliberately not started here. Streaming silence while nothing plays
        # makes every screen redraw pop; see STREAM_LINGER_MS.
        self._streaming = False
        self._last_sound = 0
        # Counted rather than raised: see trigger().
        self.audio_errors = 0
        self.last_audio_error = None

        self._samples = [None] * TRACK_COUNT
        # Everything a playing sample reads through, held for as long as it
        # can play. CircuitPython's audio objects keep pointers into these
        # buffers, not references the collector can trace, so a buffer that
        # is only a local is collectable the moment it goes out of scope -
        # and the audio then reads whatever took its place. That is loud
        # garbage at best and a hard fault at worst. See _load_to_ram.
        #
        # Both the bytes and the memoryview over them are kept. A memoryview
        # in MicroPython does not necessarily keep its base object alive, so
        # holding only the view can still leave the bytes collectable, and
        # holding only the bytes relies on the sample pointing at them rather
        # than at the view. Holding both costs two slots in a list.
        self._audio = [None] * TRACK_COUNT
        self._views = [None] * TRACK_COUNT
        # The read buffer a streamed track's WaveFile reads through.
        self._stream_buffers = [None] * TRACK_COUNT
        # An auditioned sample is owned by nothing else; it and its buffer
        # have to live here until the next audition replaces them.
        self._audition_sample = None
        self._audition_buffer = None
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
        self._last_usb_midi_poll = 0
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
        except (OSError, wav.WavError, MemoryError):
            # MemoryError is caught deliberately. It is neither OSError nor
            # WavError, so without this a corrupt header escapes to the main
            # loop, and the default kit loads at import - which would fail the
            # badge on every boot rather than merely silencing one track.
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

        sample, audio, view = self._load_to_ram(handle, offset, size)
        if sample is not None:
            handle.close()
            self._samples[track] = sample
            self._audio[track] = audio
            self._views[track] = view
            self._streamed[track] = False
            self._sizes[track] = size
        else:
            try:
                handle.seek(0)
                buffer = bytearray(STREAM_BUFFER)
                self._samples[track] = WaveFile(handle, buffer)
                self._stream_buffers[track] = buffer
            except (OSError, ValueError, MemoryError):
                # MemoryError as well: read_format only checks the chunk
                # headers it needs, so a file it accepts can still upset
                # CircuitPython's own WAV parser here. This runs for every
                # kit sample too big for RAM, and the kit loads at import,
                # so an escape from this handler fails the badge at boot.
                handle.close()
                return False
            self._files[track] = handle
            self._streamed[track] = True

        self.song.set_sample(track, path)
        return True

    def _load_to_ram(self, handle, offset, size):
        """Read the audio into memory.

        Returns (sample, buffer), or (None, None) to fall back to streaming.
        The buffer is returned rather than dropped because the caller has to
        keep it: see the comment on the RawSample below.
        """
        if size > MAX_RAM_SAMPLE or self._ram_used + size > RAM_BUDGET:
            return None, None, None
        try:
            handle.seek(offset)
            data = handle.read(size)
        except (OSError, MemoryError):
            return None, None, None
        if len(data) < size:
            return None, None, None
        try:
            # RawSample infers bit depth from the buffer's element size: raw
            # bytes mean 8-bit, which the mixer rejects at play() with "the
            # sample's bits_per_sample does not match". Casting to 16-bit
            # signed says what the audio actually is. A memoryview rather than
            # array.array('h', data) so the audio is not copied - a second
            # copy of every sample would double peak memory during loading.
            # The memoryview is named and kept, not built inline. It is what
            # the sample was actually handed, and holding only the bytes
            # underneath it relies on an assumption about which of the two
            # CircuitPython keeps a pointer into. Holding the view holds both.
            view = memoryview(data).cast("h")
            sample = RawSample(
                view,
                channel_count=CHANNELS,
                sample_rate=SAMPLE_RATE,
            )
        except (ValueError, MemoryError, TypeError):
            # TypeError as well: cast("h") rejects a buffer whose length is not
            # a multiple of two, and like MemoryError it is not caught by the
            # handlers above, so it would reach the main loop.
            return None, None, None
        self._ram_used += size
        # Both go back to the caller deliberately. The sample refers to
        # this memory and the I2S DMA reads it for as long as the sample can
        # play, but nothing here is a reference the garbage collector can
        # see: once the last name for `data` goes out of scope the bytes are
        # collectable, and playing a sample whose buffer has been reused is a
        # read of memory that is now something else. That is a hard fault,
        # not an exception - the badge drops to safe mode with no traceback,
        # which is what it did.
        return sample, data, view

    def is_streamed(self, track):
        """True when a track plays from storage rather than RAM."""
        return self._streamed[track]

    @property
    def ram_used(self):
        return self._ram_used

    def load_demo_pattern(self):
        """A plain beat, so Play does something on a badge straight out of a box.

        Deliberately simple and easy to take apart: four on the floor, a
        backbeat, and offbeat toms. Function plus a Volume click clears a
        track when it is in the way.
        """
        song = self.song
        song.clear_all()
        song.set_length(16)
        song.set_division(3)  # 1/16
        for step in (0, 4, 8, 12):
            song.set_step(0, step, 110)
        for step in (4, 12):
            song.set_step(1, step, 100)
        for step in (2, 6, 10, 14):
            song.set_step(2, step, 70)
        return song

    def load_kit(self, paths):
        loaded = 0
        for track in range(TRACK_COUNT):
            path = paths[track] if track < len(paths) else None
            if self.load_track(track, path):
                loaded += 1
        return loaded

    def load_song(self, song):
        """Play a different song, with its own kit.

        Everything sounding is stopped first. The tracks are about to point
        at other samples, and a mixer voice holds a raw pointer into the
        buffer it is playing rather than a reference the collector knows
        about - so letting go of a buffer while a voice still walks it is a
        hard fault with no traceback.
        """
        self.transport.stop()
        for track in range(TRACK_COUNT):
            self.silence_track(track)
        self.song = song
        self.page = 0
        self.clock.set_bpm(song.bpm)
        # After the song is in place: a failed sample leaves that track
        # silent, and the pattern is still the one the player asked for.
        return self.load_kit(song.kit)

    def _voices_of(self, track):
        """Every mixer voice this track can be playing on."""
        base = track * VOICES_PER_TRACK
        return range(base, base + VOICES_PER_TRACK)

    def silence_track(self, track):
        """Stop anything this track is sounding, and forget its levels.

        Called before a track's audio is let go. A mixer voice holds a raw
        pointer into the buffer it is playing, so dropping that buffer while
        the DMA is still walking it is the fault this rework exists to
        eliminate - the badge takes a hard fault with no traceback and only
        unplugging it recovers.

        Nothing reached this until now, because the kit loaded once at boot
        and was never replaced. Assigning a sample from the browser is what
        makes it reachable.
        """
        for index in self._voices_of(track):
            voice = self.mixer.voice[index]
            try:
                if voice.playing:
                    voice.stop()
            except OSError:
                # The audio path is allowed to fail; see trigger().
                pass
            self._voice_velocity[index] = 0

    def _release_track(self, track):
        # Before anything is dropped: whatever is playing reads through it.
        self.silence_track(track)
        if self._samples[track] is not None and not self._streamed[track]:
            # Reclaim the budget this track's audio was holding.
            self._ram_used = max(0, self._ram_used - self._sample_bytes(track))
        self._sizes[track] = 0
        self._streamed[track] = False
        self._samples[track] = None
        # Released only after the sample, so a buffer never outlives its
        # owner in the other direction either.
        self._audio[track] = None
        self._views[track] = None
        self._stream_buffers[track] = None
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
        # Use the current voice and advance afterwards, so a track's first hit
        # lands on its first voice. Incrementing first would start on the
        # second one, which is harmless in sound but makes the mapping
        # needlessly surprising to anyone reading a voice index.
        voice = base + self._next_voice[track]
        self._next_voice[track] ^= 1
        return voice

    def start_stream(self):
        """Begin streaming if it is not already running."""
        self._last_sound = ticks_ms()
        if self._streaming:
            return False
        self.audio.play(self.mixer)
        self._streaming = True
        return True

    def stop_stream(self):
        if not self._streaming:
            return False
        self.audio.stop()
        self._streaming = False
        return True

    @property
    def streaming(self):
        return self._streaming

    def _anything_sounding(self):
        for voice in self.mixer.voice:
            if voice.playing:
                return True
        return False

    def _update_stream(self, now):
        """Stop streaming once nothing has sounded for a while."""
        if not self._streaming:
            return
        if self.transport.playing or self._anything_sounding():
            self._last_sound = now
            return
        if ticks_diff(now, self._last_sound) >= STREAM_LINGER_MS:
            self.stop_stream()

    def trigger(self, track, velocity):
        """Sound one hit. Used by both the sequencer and live pads.

        A hit that will not sound must not stop the sequencer. Observed on
        the badge: `voice.play` raised OSError(EIO) from inside the main
        loop, which ended the program and left the instrument dead in the
        middle of a pattern. The audio path runs on peripherals - I2S, DMA,
        and whatever the background tasks are doing to the same buses - so it
        can fail underneath a call that looks like pure computation. Losing
        one drum hit is recoverable; losing the badge is not.
        """
        sample = self._samples[track]
        if sample is None:
            return False
        # Resolved once: in polyphonic mode _voice_for advances the track's
        # rotation, so asking twice would set the level on one voice and
        # remember the velocity against the next.
        index = self._voice_for(track)
        voice = self.mixer.voice[index]
        voice.level = self.volume * (velocity / 127.0)
        self._voice_velocity[index] = velocity
        try:
            # Starting here means the stream's own transient lands under a
            # drum hit rather than in silence, where it would be obvious.
            self.start_stream()
            voice.play(sample)
        except OSError as error:
            self.audio_errors += 1
            self.last_audio_error = error
            # Silence the output before returning. Observed on the badge:
            # containing the error but leaving the stream up left the I2S
            # peripheral looping whatever was in its buffer, which is a loud
            # continuous noise rather than a missed drum hit. Stopping it
            # costs the next hit a stream start, which is the cheaper of the
            # two by a wide margin.
            try:
                self.stop_stream()
            except OSError:
                # The teardown is best effort; the path is already faulty.
                pass
            # Say so once. A silent skip would turn a hardware fault into a
            # pattern that quietly drops hits, which is far harder to chase.
            if self.audio_errors == 1:
                print("audio error on track %d: %r" % (track, error))
            return False
        if self.midi_out[track]:
            self._send_midi(track, velocity)
        return True

    def audition(self, path):
        """Preview a sample without disturbing any track."""
        try:
            handle = open(path, "rb")
        except OSError:
            return False
        buffer = bytearray(STREAM_BUFFER)
        try:
            sample = WaveFile(handle, buffer)
        except (OSError, ValueError, MemoryError):
            # Close it here: a browser paging through a card full of bad files
            # would otherwise leak a descriptor for every one of them.
            handle.close()
            return False
        self.start_stream()
        voice = self.mixer.voice[AUDITION_VOICE]
        # Half a full-velocity hit, so a preview sits under the pattern.
        voice.level = self.volume * 0.5
        self._voice_velocity[AUDITION_VOICE] = MAX_VELOCITY // 2
        # Held before playing, not after: the sample and the buffer it reads
        # through must outlive this function, and nothing else owns them.
        self._audition_sample = sample
        self._audition_buffer = buffer
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
            self.start_stream()
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
            self.clock.tick,
            self.song.ticks_per_step,
            self.song.track_length(self.selected_track),
        )
        self.song.set_step(track, step, DEFAULT_VELOCITY, offset)
        return step

    def erase(self, track):
        """Live erase: clear this track's hit at the current position."""
        step = quantize.nearest_step(
            self.clock.tick
            % (self.song.track_length(self.selected_track) * self.song.ticks_per_step),
            self.song.ticks_per_step,
            self.song.track_length(self.selected_track),
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
            self.poll_midi_in(now)
        self._update_sync_out(now)

        fired = self.clock.update(now)
        for _ in range(fired):
            self._on_tick(now)
        self._update_stream(now)

    def _on_tick(self, now):
        tick = self.clock.tick
        for track, _step, velocity in quantize.hits_due(self.song, tick, self.strength):
            self.trigger(track, velocity)
        if self.clock.sync_out_due(tick):
            self._pulse_sync_out(now)

    @property
    def current_step(self):
        total = self.song.track_length(self.selected_track) * self.song.ticks_per_step
        return (self.clock.tick % total) // self.song.ticks_per_step

    # --- sync -------------------------------------------------------------

    def poll_midi_in(self, now=None):
        """Act on MIDI transport messages from either port.

        Start, Stop and Continue are the standard way a sequencer is driven
        remotely, and they give an explicit play intent that an anonymous
        stream of sync pulses cannot: the badge can stay out of the way of a
        clock it is merely listening to, while still obeying a real Start.
        """
        # receive() allocates whether or not a message arrives: measured on
        # the badge at 32 bytes a call for the serial port and 64 for USB.
        # Called on a 2 ms timer that is about 15 KB of garbage a second, and
        # the heap it churns through is the same heap the audio path needs -
        # free memory was seen dipping to a couple of hundred bytes while a
        # pattern played. Nothing was connected to either port.
        #
        # in_waiting is the cheap probe for "is there anything at all", and
        # it is only asked once. It cannot be the loop condition, because it
        # describes the UART's buffer and not the parser's: receive() slurps
        # every available byte in one go, so a burst carrying two messages
        # leaves in_waiting at zero with the second still held inside
        # adafruit_midi. Gating on it there drops that second message until
        # more bytes happen to arrive, which is late enough to look like the
        # badge answering the previous press - measured, a Stop and a Start in
        # one burst left the transport stopped when it should have been
        # playing, and the pair the other way round left it playing.
        if midi_uart.in_waiting:
            for _ in range(MAX_MIDI_PER_POLL):
                if not self._handle_midi(midi_serial.receive(), now):
                    # Nothing more decoded: either empty, or the rest of a
                    # message has not arrived yet and asking again only spins.
                    break

        # USB has no equivalent - PortIn offers only read and readinto - so it
        # is polled on its own slower timer instead, and drained when it does
        # get polled. Transport messages are rare and a few milliseconds late
        # costs nothing. A clock over USB is a different matter: the ticks
        # will be right, because one clock is one tick however late it is
        # read, but their timing is only as good as this interval. A hardware
        # MIDI lead, polled every 2 ms above, is the accurate way in.
        #
        # `now` comes from the caller, which already has it: asking for the
        # time again would be another call on the hottest path in the loop.
        if now is None:
            now = ticks_ms()
        if ticks_diff(now, self._last_usb_midi_poll) >= USB_MIDI_POLL_MS:
            self._last_usb_midi_poll = now
            for _ in range(MAX_MIDI_PER_POLL):
                if not self._handle_midi(midi_usb.receive(), now):
                    break

    def _handle_midi(self, message, now=None):
        """Act on one message. Returns whether there was one.

        The answer is what lets the caller stop draining: None means the port
        had nothing, or had part of something whose remaining bytes have not
        arrived.
        """
        if message is None:
            return False
        if isinstance(message, TimingClock):
            # 24 a quarter note, fixed by the standard, which is exactly this
            # engine's tick rate - so one clock is one tick. Told to the clock
            # explicitly rather than left to the jack's setting, because the
            # two can differ and only one of them is a MIDI cable.
            if now is None:
                now = ticks_ms()
            self.clock.external_pulse(now, ppqn=MIDI_CLOCK_PPQN)
            if not self.transport.playing and self.sync_starts_transport:
                self.transport.start()
                self.clock.start(now)
        elif isinstance(message, Start):
            self._remote_start(reset=True)
        elif isinstance(message, Continue):
            self._remote_start(reset=False)
        elif isinstance(message, Stop):
            if self.transport.playing:
                self.transport.stop()
                self.clock.stop()
        return True

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

    @property
    def volume_percent(self):
        """Where the knob is, as a percentage of its travel.

        Not the level: the level is a decibel curve, so at the quiet end it
        is a number like 0.005, and a display showing 0 or 1 for the bottom
        third of the dial tells the player nothing about where the knob is.
        """
        return int(round(100.0 * self.volume_position / VOLUME_STEPS))

    def set_volume_position(self, position):
        """Move the knob to a notch, and apply the level that notch means."""
        self.volume_position = int(clamp(position, 0, VOLUME_STEPS))
        return self.set_volume(level_for_position(self.volume_position))

    def set_volume(self, value):
        """Set the master volume, and apply it to whatever is sounding.

        Applying it immediately is the point. A drum hit is a third of a
        second, so waiting for the next one would usually be quick enough -
        but "usually" is not good enough for something whose job is to stop
        a sound that is too loud in someone's ears.
        """
        # Snap to a notch, so the level and the knob position can never
        # disagree. Storing the value as given left them describing
        # different things: the screen showed the position, the voices got
        # the raw level, and the next detent moved from the position - so a
        # level set from anywhere but the knob made all three diverge.
        self.volume_position = position_for_level(clamp(value, MIN_VOLUME, MAX_VOLUME))
        self.volume = level_for_position(self.volume_position)
        for index in range(MIXER_VOICES):
            velocity = self._voice_velocity[index]
            if velocity:
                self.mixer.voice[index].level = self.volume * (velocity / 127.0)
        return self.volume

    def nudge_volume(self, steps, now=None):
        """Move the volume by that many detents, scaled by how fast it turned.

        The count matters rather than just the direction: a hand spinning the
        knob produces a large delta in one pass of the loop. So does the gap
        since it last moved - creeping it round gives fine control, spinning
        it covers the range in one movement. See engine.util.accelerated.
        """
        if now is None:
            now = ticks_ms()
        elapsed = None
        if self._last_volume_turn is not None:
            elapsed = ticks_diff(now, self._last_volume_turn)
            if elapsed < 0:
                elapsed = None
        self._last_volume_turn = now
        return self.set_volume_position(
            self.volume_position + accelerated(steps, elapsed)
        )

    def set_bpm(self, value):
        return self.clock.set_bpm(value)

    def select_track(self, track):
        self.selected_track = track % TRACK_COUNT
        return self.selected_track

    def toggle_mode(self):
        self.mode = SEQ if self.mode == LIVE else LIVE
        return self.mode

    def set_page(self, page):
        pages = self.song.page_count_for(self.selected_track)
        self.page = page % max(1, pages)
        return self.page


# The singleton. Importing this module allocates the audio path once, for the
# lifetime of the program. Nothing streams until something is played.
engine = Sequencer()
engine.load_kit(DEFAULT_KIT)
engine.load_demo_pattern()
