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
import gc
from adafruit_midi.midi_continue import Continue
from adafruit_midi.note_on import NoteOn
from adafruit_midi.start import Start
from adafruit_midi.stop import Stop
from adafruit_midi.timing_clock import TimingClock
from audiocore import RawSample
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
from engine.song import (
    DEFAULT_LENGTH,
    DEFAULT_VELOCITY,
    MAX_VELOCITY,
    TRACK_COUNT,
    Song,
)
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

# Loaded at startup so the badge makes a sound out of the box. Bare names, so
# they resolve wherever the samples actually live - card first, then flash.
#
# These four are from the Kosmo drums set, converted to the mixer's format
# (16 kHz, mono, 16-bit signed) because the mixer sums voices rather than
# resampling them and rejects any sample whose rate differs.
#
# A closed and an open hat rather than a crash: shared four ways the budget
# is a quarter of a second a track, and a cymbal is the one sound that says
# nothing at all in a quarter of a second. A hat pair is the opposite - it is
# most of what a beat needs and both halves are already that short.
#
# The four were trimmed to 0.25 s with
# `Tools/convert_samples.py --max-seconds 0.25`, so the four together spend
# 31,940 of RAM_BUDGET's 32,768 bytes and every track sounds. Trimmed offline
# rather than at load, because the fade is then chosen once and by ear; the
# runtime does the same thing to anything the player picks, as a safety net.
# Retune by ear if the balance is wrong - the arithmetic is just seconds
# times 32,000 bytes.
DEFAULT_KIT = (
    "kick_crater.wav",  # Track 1
    "snare_kraken-head_1.wav",  # Track 2
    "hh_hats-closed_1.wav",  # Track 3
    "hh_hats-open_1.wav",  # Track 4
)

# Samples are held in RAM. Nothing streams from storage, ever.
#
# Streaming was tried and removed. CircuitPython refills a playing WaveFile
# from a background callback, and `busio` SPI spins RUN_BACKGROUND_TASKS
# inside its own transfer loop - so any main-thread card access while a stream
# is live re-enters FatFS underneath itself. The card error is then latched
# into the file handle for good, `audiomixer` has no branch for a failed
# refill and quietly sets the voice's sample to NULL, and the track is dead
# with nothing raised. Measured on the badge: a streamed track died on its
# second hit every time, where the same pattern from RAM survived 40 of 40.
# See docs/streaming-bug-rootcause.md.
#
# Holding the audio takes storage out of the audio path completely, which is
# the only configuration that never failed. The cost is length, and it is paid
# by trimming: a sample longer than its share is loaded head first and faded
# out rather than streamed or refused.
#
# Both numbers were measured against the ~86 KB free with the engine loaded,
# and both are deliberately conservative. The binding constraint is not total
# free memory but the largest contiguous block - 11840 bytes of 25264 free,
# measured after a kit was loaded - so re-measure on hardware before raising
# either.
# Measured on the badge, at the moment that decides it: StartupState prints
# free memory once the whole UI is warmed. With a 48 KB budget that line read
#
#     free after warm: 17120, kit 49152
#
# and 17 KB is not enough to open the sample browser - 98 paths and then 99
# menu rows at 165 bytes each need something like 25 KB at peak. The listing
# failed for want of memory, was remembered as "there are no samples", and the
# browser showed nothing but "(none)" for the rest of the session.
#
# So the budget is what is left over after the rest of the badge has what it
# needs, not the other way round. 32 KB leaves about 33 KB free, which covers
# the browser with room to spare.
#
# The cost is length: shared across a four-track kit this is 0.256 s a track.
# Streaming from internal flash is what buys that back without spending RAM -
# see docs/streaming-bug-rootcause.md, option B - and it is the right next
# move if sample length matters more than the simplicity of holding
# everything in memory.
RAM_BUDGET = 32 * 1024
MAX_RAM_SAMPLE = 24 * 1024

# The least a sample is worth loading. Below this there is not enough of a
# sound left to recognise, so the track is left silent and says why rather
# than firing a click on every step.
MIN_RAM_SAMPLE = 2 * 1024

# Bytes per frame at the mixer's format: mono, 16-bit. Reads and trims are
# rounded to whole frames, because half a frame makes memoryview.cast("h")
# raise and puts the rest of the sample an octave sideways.
FRAME_BYTES = 2

# How much of a sample a preview holds. Deliberately less than a track's
# share: an audition is allocated on top of an already loaded kit, when the
# heap is at its most fragmented, and long enough to recognise a sound is all
# a preview has to be.
AUDITION_BYTES = 12 * 1024

# How long the fade on a trimmed tail runs for, in frames.
#
# A sample cut mid-waveform ends on a full-scale step, and a step is a click -
# the same discontinuity that made a single-voice retrigger audible on the
# badge. Fading the last few milliseconds costs a sound nobody can hear and
# removes one everybody can. Eight milliseconds.
#
# Tools/convert_samples.py --max-seconds does the same thing offline, which is
# where a shipped kit should be trimmed - by ear, once. This is the safety net
# for whatever the player picks at runtime.
FADE_FRAMES = SAMPLE_RATE * 8 // 1000

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

# How long the volume knob has to sit still before where it was left is
# written to the card.
#
# Not per detent: a hand crossing the range produces dozens, and each would be
# a file write. Not immediately either - the write is tens of milliseconds
# against a 32 ms audio buffer, so it waits for a moment when nothing is
# sounding as well. Between them that is one write per adjustment, landing in
# a silence rather than under a drum hit.
VOLUME_SAVE_MS = 1500

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

    A MemoryError is deliberately not caught. Turning "I could not read the
    listing" into "there are no samples" is a lie the caller cannot tell from
    the truth, and SettingsState.Catalog remembers what this returns for the
    rest of the session - so one tight moment during boot used to leave the
    browser showing nothing but "(none)" until the badge was power cycled.
    Reported from the badge as "I went to add a sample to track 5 and it just
    says none". Raising instead lets the caller decline to remember it, and
    lets the screen say what actually happened.
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
        except OSError:
            # A directory that is not there. A badge with no card is not an
            # error, so the next store gets its turn.
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


def _track_of_voice(index):
    """Which track owns a mixer voice, or None for the floating ones.

    The strip is laid out as VOICES_PER_TRACK per track and then the
    audition and metronome on the end, so this is arithmetic rather than a
    lookup - but it is arithmetic that has to agree with _voices_of.
    """
    if index >= AUDITION_VOICE:
        return None
    return index // VOICES_PER_TRACK


def _listdir(directory):
    from os import listdir

    return listdir(directory)


def resolve_sample(name, lister=None, dirs=None):
    """The first path a bare filename refers to, or None."""
    candidates = sample_candidates(name, lister, dirs)
    return candidates[0] if candidates else None


def _fade_tail(view, frames=FADE_FRAMES):
    """Ramp the end of a trimmed sample down to silence, in place.

    Integer arithmetic on purpose. This runs for every trimmed sample the
    browser loads, on a board with no floating point unit, where the few
    hundred frames it touches would otherwise cost more than the read did.
    """
    total = len(view)
    if frames > total:
        frames = total
    if frames < 2:
        return
    start = total - frames
    for index in range(frames):
        # The last frame is scaled by zero, so the sample ends in silence
        # rather than merely quietly.
        view[start + index] = view[start + index] * (frames - index - 1) // frames


def _read_audio(handle, wanted, floor=MIN_RAM_SAMPLE):
    """Read `wanted` bytes into a fresh buffer, or None.

    Whether the bytes are affordable is the budget's question; whether they
    can actually be allocated is a different one. What runs out first on this
    board is not free memory but the largest contiguous block - 11840 bytes of
    25264 free, measured after a kit was loaded. So a sample the budget allows
    can still fail to allocate once the badge has been running, which is
    exactly when the sample browser gets used.

    Rather than report the track silent, collect once and then ask for less,
    halving down to `floor`. A shorter sound is a much better answer than no
    sound, and the caller fades whatever it gets.
    """
    if floor > wanted:
        # A file smaller than the floor is simply a short sound. The floor
        # exists to stop the halving below returning something too brief to
        # recognise, not to set a minimum length.
        floor = wanted
    collected = False
    while wanted >= floor:
        try:
            buffer = bytearray(wanted)
        except MemoryError:
            if not collected:
                # One collection, not one per attempt: the whole point of
                # giving ground is to stop asking for what is not there.
                gc.collect()
                collected = True
                continue
            wanted //= 2
            wanted -= wanted % FRAME_BYTES
            continue
        try:
            read = handle.readinto(buffer)
        except OSError:
            return None
        if read is None or read < wanted:
            # A file shorter than its own header claims. Refuse it rather than
            # play the uninitialised tail of the buffer, which is noise.
            return None
        return buffer
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
        # When the volume knob last moved, so its speed can be read back -
        # and whether where it was left still has to reach the card.
        self._last_volume_turn = None
        self._volume_unsaved = False

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
        # Which tracks have sounded since anyone last looked, as a bitmask.
        # A bitmask rather than a set because this is written on every hit
        # and read on every pass of the display: an int costs no allocation
        # where a set costs one per hit, and there are only eight tracks.
        self._hits = 0
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
        # An auditioned sample is owned by nothing else; it, its bytes and the
        # view over them have to live here until the next audition replaces
        # them, for the same reason the tracks hold all three.
        self._audition_sample = None
        self._audition_buffer = None
        self._audition_view = None
        # How many bytes of audio each track actually holds, which is what the
        # budget is spent in - not the size of the file it came from.
        self._sizes = [0] * TRACK_COUNT
        # Which tracks hold less than their file, so the badge can say so
        # rather than leaving a player hunting for the tail of their crash.
        self._truncated = [False] * TRACK_COUNT
        self._ram_used = 0
        # How many tracks of a kit have still to load, or 0 when no kit is
        # loading. Read by _allowance to share the budget out.
        self._loading_kit = 0
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
        # Cleared first, so what is left here describes this load and no
        # earlier one. SettingsState reads it to tell "no room" from "will not
        # play", and a leftover from another track answered for both.
        self.last_error = None
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
            try:
                rate, channels, bits, offset, size = wav.read_format(handle)
            except (OSError, wav.WavError, MemoryError):
                # MemoryError is caught deliberately. It is neither OSError nor
                # WavError, so without this a corrupt header escapes to the main
                # loop, and the default kit loads at import - which would fail the
                # badge on every boot rather than merely silencing one track.
                return False

            if not wav.matches(rate, channels, bits, SAMPLE_RATE, CHANNELS, BITS):
                # The mixer has one fixed format; anything else plays at the wrong
                # pitch or not at all. Refuse it rather than make a mess of it.
                self.last_error = "%s is %s, need %s" % (
                    path,
                    wav.describe(rate, channels, bits),
                    wav.describe(SAMPLE_RATE, CHANNELS, BITS),
                )
                return False

            if not self._load_to_ram(track, path, handle, offset, size):
                return False
        finally:
            # Closed on every path out, including the successful one. Nothing
            # here outlives the load: the audio is copied into memory the track
            # owns, so no handle is kept and storage is never touched again
            # while the sample plays. That is the whole point of the RAM path.
            handle.close()

        self.song.set_sample(track, path)
        return True

    def _allowance(self):
        """How many bytes the next sample may take.

        While a whole kit is loading, what is left of the budget is divided by
        the tracks that have still to load, so an early sample cannot starve a
        late one. Measured against the shipped kit read off the card - 131 KB
        of samples against a 48 KB budget - first come, first served loaded
        three tracks and left the cymbal silent. Sharing gives 0.38, 0.25,
        0.45 and 0.45 seconds instead: every pad sounds, which is the whole
        point of trimming rather than refusing.

        Unspent share is not lost. The room is recomputed from what has
        actually been used, so a short sample leaves its remainder to the
        tracks after it.

        Outside a kit load - the browser assigning one sample - there is
        nothing to share with, and the track may take whatever is free.
        """
        room = RAM_BUDGET - self._ram_used
        cap = MAX_RAM_SAMPLE
        if self._loading_kit > 1:
            share = room // self._loading_kit
            if share < cap:
                cap = share
        return room if room < cap else cap

    def _load_to_ram(self, track, path, handle, offset, size):
        """Read the audio into memory, trimming it if it will not all fit.

        A sample longer than its share is loaded head first and faded out. The
        alternative used to be streaming it, which is what this rework
        removed; the alternative before that was refusing it, which turned a
        long sound into a silent track. A shortened crash is the best of the
        three and the only one that always plays.

        Everything that can fail does so before any track state is touched, so
        a failed load leaves the track exactly as the release left it.
        """
        allowance = self._allowance()
        if size <= allowance:
            # It fits. A sample shorter than MIN_RAM_SAMPLE is not a problem -
            # a rim or a click is meant to be brief, and the floor below is
            # about how much room is worth trimming into, not how long a sound
            # is allowed to be.
            wanted = size
        else:
            if allowance < MIN_RAM_SAMPLE:
                self.last_error = "%s: no room left in the sample budget" % path
                return False
            wanted = allowance
        wanted -= wanted % FRAME_BYTES
        if wanted < FRAME_BYTES:
            return False
        try:
            handle.seek(offset)
        except OSError:
            return False
        audio = _read_audio(handle, wanted)
        if audio is None:
            return False
        try:
            # RawSample infers bit depth from the buffer's element size: raw
            # bytes mean 8-bit, which the mixer rejects at play() with "the
            # sample's bits_per_sample does not match". Casting to 16-bit
            # signed says what the audio actually is. The view is also what
            # makes the fade possible - a bytearray read through a cast view
            # is writable, where the bytes returned by read() would not be.
            #
            # The memoryview is named and kept, not built inline. It is what
            # the sample was actually handed, and holding only the bytes
            # underneath it relies on an assumption about which of the two
            # CircuitPython keeps a pointer into. Holding the view holds both.
            view = memoryview(audio).cast("h")
        except (ValueError, TypeError):
            # TypeError as well: cast("h") rejects a buffer whose length is not
            # a multiple of two, and like MemoryError it is not caught by the
            # handlers above, so it would reach the main loop.
            return False
        trimmed = len(audio) < size
        if trimmed:
            _fade_tail(view)
        try:
            sample = RawSample(
                view,
                channel_count=CHANNELS,
                sample_rate=SAMPLE_RATE,
            )
        except (ValueError, MemoryError, TypeError):
            return False
        self._samples[track] = sample
        # Both the bytes and the view go into the track deliberately. The
        # sample refers to this memory and the I2S DMA reads it for as long as
        # the sample can play, but nothing here is a reference the garbage
        # collector can see: once the last name for the buffer goes out of
        # scope the bytes are collectable, and playing a sample whose buffer
        # has been reused is a read of memory that is now something else. That
        # is a hard fault, not an exception - the badge drops to safe mode with
        # no traceback, which is what it did.
        self._audio[track] = audio
        self._views[track] = view
        self._sizes[track] = len(audio)
        self._truncated[track] = trimmed
        self._ram_used += len(audio)
        return True

    def was_truncated(self, track):
        """True when this track holds less audio than its file had.

        Worth surfacing rather than hiding: the sound is the head of the file
        with a fade on it, not the file, and a player wondering where the tail
        of their crash went deserves to be told.
        """
        return self._truncated[track]

    @property
    def ram_used(self):
        return self._ram_used

    def restore(self):
        """Come up as the badge was left, or as a new badge if it was not.

        Three things in order, each allowed to fail on its own: the song last
        saved or loaded, then the samples last assigned over the top of it,
        and the shipped kit and demo pattern if there was no song to find.

        Nothing here may raise. This runs at import, so an escape is a badge
        that will not start - and the things it reads are on a card that can
        be taken out, written by another machine, or simply not there.

        The samples are applied after the song because they are the more
        recent statement: a song remembers the kit it was saved with, and a
        player who has swapped a sample since then meant the swap.
        """
        import prefs
        import songfile

        # The knob first: it costs one read and decides how loud the first
        # thing the badge does is.
        saved = prefs.volume_position()
        if saved != prefs.NO_VOLUME:
            self.set_volume_position(saved)

        song = None
        name = prefs.last_song()
        if name:
            try:
                song = songfile.load(name)
                song.name = name
            except Exception:
                # Deliberately everything. A song file is untrusted input off
                # a card, songfile raises StoreError, the card raises OSError,
                # and a corrupt one can raise things neither of them names.
                song = None
        if song is not None:
            try:
                self.load_song(song)
            except Exception:
                # Same reasoning as the load above, and the same "nothing here
                # may raise": a song is a dictionary off a card, and every
                # field it puts back into the engine is one more thing that
                # has to be exactly the shape the engine expects. Falling back
                # to the demo is a badge that plays; letting this out is a
                # badge that does not start.
                song = None
        if song is None:
            self.load_kit(DEFAULT_KIT)
            self.load_demo_pattern()

        kit = prefs.last_kit()
        # `is not None`, not truthiness: a list of eight Nones is a player who
        # silenced every track and meant it, and coming back making noise
        # would be ignoring them. prefs.last_kit returns None - not an empty
        # list - when nothing has been remembered at all.
        if kit is not None:
            try:
                self.load_kit(kit)
                for track in range(TRACK_COUNT):
                    self.song.set_sample(
                        track, kit[track] if track < len(kit) else None
                    )
            except Exception:
                # Same reasoning: a remembered path is whatever was on the
                # card last time, and the badge has to boot either way.
                pass
        return song is not None

    def load_demo_pattern(self):
        """A plain beat, so Play does something on a badge straight out of a box.

        Deliberately simple and easy to take apart: a kick, a backbeat and
        offbeat toms. Function plus a Volume click clears a track when it is
        in the way.

        Eight steps, matching what a new song is, so the first thing the
        badge plays is the same shape as the first thing you would write -
        and the whole of it is on one page of pads.
        """
        song = self.song
        song.clear_all()
        song.set_length(DEFAULT_LENGTH)
        song.set_division(3)  # 1/16
        for step in (0, 4):
            song.set_step(0, step, 110)
        song.set_step(1, 4, 100)
        for step in (2, 6):
            song.set_step(2, step, 70)
        return song

    def assign_sample(self, track, path):
        """Point one track at a sample, making room for it if there is none.

        A kit spends the whole budget between the tracks that have samples,
        so adding a sample to a track that had none finds nothing left over -
        and refusing reads as "this sample is broken" when it is only "the
        others have taken it all". Reloading the whole kit shares the budget
        out again over one more track, and everything gets a little shorter.

        The cost is a re-read of every sample in the kit, which is most of a
        second off a card. It is paid only when the sample does not simply
        fit, and only when the player has just asked for it - by which point
        they are already waiting for something to happen.

        Clearing a track needs none of this: letting go only ever frees room.
        """
        self.song.set_sample(track, path)
        if not path:
            self.load_track(track, None)
            return True
        if self.load_track(track, path):
            return True
        # No room. Everything reloads, this track included, so the budget is
        # divided by the tracks that now want it rather than by the ones that
        # wanted it before.
        self.load_kit(self.song.kit)
        return self.has_sample(track)

    def load_kit(self, paths):
        """Point every track at its sample, sharing the budget between them.

        The count is taken first so _allowance knows how many tracks are still
        to come; it is decremented per track rather than per success, because
        a track that fails to load has still had its turn and its share should
        go back to the others.
        """
        wanted = [
            paths[track] if track < len(paths) else None for track in range(TRACK_COUNT)
        ]
        # Everything is released before anything is loaded. Releasing each
        # track only as its turn came left the budget still held by the tracks
        # behind it, so the share worked out from what was free was a fraction
        # of a fraction, and the first track could not fit its own sample.
        # load_track releases each one anyway; doing it up front is what makes
        # the arithmetic below describe the whole kit rather than the tail of
        # it.
        for track in range(TRACK_COUNT):
            self._release_track(track)
        self._loading_kit = sum(1 for path in wanted if path)
        loaded = 0
        try:
            for track in range(TRACK_COUNT):
                if self.load_track(track, wanted[track]):
                    loaded += 1
                if wanted[track] and self._loading_kit:
                    self._loading_kit -= 1
        finally:
            # Cleared however this ends: a raise here would otherwise leave
            # every later single-track load capped at a share of a kit that is
            # no longer loading.
            self._loading_kit = 0
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
        if self._samples[track] is not None:
            # Reclaim the budget this track's audio was holding.
            self._ram_used = max(0, self._ram_used - self._sample_bytes(track))
        self._sizes[track] = 0
        self._truncated[track] = False
        self._samples[track] = None
        # Released only after the sample, so a buffer never outlives its
        # owner in the other direction either.
        self._audio[track] = None
        self._views[track] = None

    def _sample_bytes(self, track):
        return self._sizes[track]

    def has_sample(self, track):
        return self._samples[track] is not None

    # --- voices -----------------------------------------------------------

    def take_hits(self):
        """Which tracks have sounded since this was last called.

        Read and cleared together, so a hit is shown once however many
        passes go by before the display next looks. Covers pads struck by
        hand and steps fired by the sequencer alike, because both arrive
        through trigger and the panel should not care which.
        """
        hits = self._hits
        self._hits = 0
        return hits

    def _level_for(self, track, velocity):
        """One voice's level: master, the track's own trim, then the hit.

        The trim is the song's if it has an opinion and the kit's otherwise,
        so a sample that is simply too loud can be fixed once with the sounds
        rather than in every song that uses them.

        `track` may be None, for the voices that belong to no track - the
        audition and the metronome - which take the master level alone.
        """
        level = self.volume * (velocity / 127.0)
        if track is None:
            return level
        return level * self.song.volume_for(track)

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

    def _save_volume(self, now):
        """Write the volume down, once the knob is still and the badge quiet.

        Two conditions, and both matter. Still, because a knob being turned
        produces a detent per pass and each write is a file. Quiet, because a
        card write is tens of milliseconds against a 32 ms buffer, and a tick
        in the middle of a pattern is a poor price for remembering something
        nobody is waiting on.

        A badge switched off mid-pattern without ever stopping it forgets the
        change. That is the trade: the alternative is hearing every one.
        """
        if not self._volume_unsaved:
            return
        if self._last_volume_turn is None:
            return
        if ticks_diff(now, self._last_volume_turn) < VOLUME_SAVE_MS:
            return
        if self.transport.playing or self._anything_sounding():
            return
        import prefs

        self._volume_unsaved = False
        try:
            prefs.set_volume_position(self.volume_position)
        except Exception:
            # Best effort, exactly as the rest of prefs is: a badge with no
            # card forgets between power-ups and must not stop over it.
            pass

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
        voice.level = self._level_for(track, velocity)
        self._voice_velocity[index] = velocity
        try:
            # Starting here means the stream's own transient lands under a
            # drum hit rather than in silence, where it would be obvious.
            self.start_stream()
            voice.play(sample)
            self._hits |= 1 << track
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
        """Preview a sample without disturbing any track.

        Held in RAM like everything a track plays, and for the same reason:
        the browser is used while a pattern is running, and reading the card
        underneath a playing voice is the fault this rework removed.
        """
        try:
            handle = open(path, "rb")
        except OSError:
            return False
        try:
            try:
                rate, channels, bits, offset, size = wav.read_format(handle)
            except (OSError, wav.WavError, MemoryError):
                return False
            if not wav.matches(rate, channels, bits, SAMPLE_RATE, CHANNELS, BITS):
                self.last_error = "%s is %s, need %s" % (
                    path,
                    wav.describe(rate, channels, bits),
                    wav.describe(SAMPLE_RATE, CHANNELS, BITS),
                )
                return False
            wanted = size if size < AUDITION_BYTES else AUDITION_BYTES
            wanted -= wanted % FRAME_BYTES
            if wanted < FRAME_BYTES:
                return False
            try:
                handle.seek(offset)
            except OSError:
                return False
            # The previous preview is let go before the next is allocated, so
            # two are never held at once on the most fragmented heap the badge
            # has. Stopped first: a mixer voice holds a raw pointer into the
            # buffer it is playing, not a reference the collector can see.
            self._stop_audition()
            audio = _read_audio(handle, wanted, FRAME_BYTES)
            if audio is None:
                return False
        finally:
            # A browser paging through a card full of unreadable files would
            # otherwise leak a descriptor for every one of them.
            handle.close()

        try:
            view = memoryview(audio).cast("h")
        except (ValueError, TypeError):
            return False
        if len(audio) < size:
            _fade_tail(view)
        try:
            sample = RawSample(
                view,
                channel_count=CHANNELS,
                sample_rate=SAMPLE_RATE,
            )
        except (ValueError, MemoryError, TypeError):
            return False
        self.start_stream()
        voice = self.mixer.voice[AUDITION_VOICE]
        # Half a full-velocity hit, so a preview sits under the pattern.
        voice.level = self.volume * 0.5
        self._voice_velocity[AUDITION_VOICE] = MAX_VELOCITY // 2
        # Held before playing, not after: the sample, its bytes and the view
        # over them must outlive this function, and nothing else owns them.
        self._audition_sample = sample
        self._audition_buffer = audio
        self._audition_view = view
        voice.play(sample)
        return True

    def _stop_audition(self):
        """Silence the preview voice and let go of what it was playing."""
        voice = self.mixer.voice[AUDITION_VOICE]
        try:
            if voice.playing:
                voice.stop()
        except OSError:
            # The audio path is allowed to fail; see trigger().
            pass
        self._voice_velocity[AUDITION_VOICE] = 0
        self._audition_sample = None
        self._audition_buffer = None
        self._audition_view = None

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
        self._save_volume(now)

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
                self.mixer.voice[index].level = self._level_for(
                    _track_of_voice(index), velocity
                )
        return self.volume

    def refresh_levels(self):
        """Push current levels at whatever is already sounding.

        A track's trim changing mid-hit should be audible on that hit rather
        than only on the next one; the whole point of turning the knob is to
        hear the balance change.
        """
        for index in range(MIXER_VOICES):
            velocity = self._voice_velocity[index]
            if velocity:
                self.mixer.voice[index].level = self._level_for(
                    _track_of_voice(index), velocity
                )

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
        # Worth writing down once the hand comes off the knob; see _save_volume.
        self._volume_unsaved = True
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
engine.restore()
