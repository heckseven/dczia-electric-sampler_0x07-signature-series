"""The sample converter, checked where it is easy to be quietly wrong.

Every sample the badge plays has to arrive at the mixer's exact format, so this
tool sits between any sound source and the badge. Its resampling used to be
plain linear interpolation, which is correct-looking code that silently
destroys the samples it is given: see test_downsampling_does_not_alias.

convert_samples lives in Tools/, which conftest puts on the path.
"""

import math
import struct
import wave

import pytest

import convert_samples


def tone(path, freq, rate=44100, seconds=1.0, amplitude=20000, channels=1):
    """Write a sine wave, so the spectrum of the result is known in advance."""
    count = int(rate * seconds)
    frames = []
    for index in range(count):
        value = int(amplitude * math.sin(2 * math.pi * freq * index / rate))
        frames.extend([value] * channels)
    return pcm(path, frames, rate=rate, channels=channels)


def pcm(path, frames, rate=44100, channels=1):
    """Write exactly these samples, for the cases where the count is the point."""
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(struct.pack("<%dh" % len(frames), *frames))
    return str(path)


def read(path):
    with wave.open(str(path), "rb") as source:
        raw = source.readframes(source.getnframes())
        values = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
        return (
            values,
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
        )


def converted(tmp_path, source, **kwargs):
    """Convert and read back, so each test shows only what it varies."""
    out = str(tmp_path / "out.wav")
    convert_samples.convert(source, out, **kwargs)
    return read(out)


def level_at(values, freq, rate):
    """Level at one frequency in dBFS, by Goertzel - no numpy in this repo."""
    if not values:
        return -999.0
    angle = 2 * math.pi * freq / rate
    coeff, first, second = 2 * math.cos(angle), 0.0, 0.0
    for value in values:
        first, second = value + coeff * first - second, first
    real = first - second * math.cos(angle)
    imaginary = second * math.sin(angle)
    magnitude = math.hypot(real, imaginary) / (len(values) / 2.0)
    return 20 * math.log10(magnitude / 32768.0) if magnitude > 0 else -999.0


def test_downsampling_does_not_alias(tmp_path):
    """Content above the new Nyquist must vanish, not fold back down.

    44100 -> 16000 moves the Nyquist to 8 kHz. A 12 kHz tone is above it, so it
    cannot be represented and has to be filtered away; if it is merely sampled
    more sparsely it reappears at |16000 - 12000| = 4 kHz. Linear interpolation
    returned that alias at -6.5 dBFS, essentially the original tone at a
    different pitch, which is what turned hats and cymbals to mush.
    """
    source = tone(tmp_path / "in.wav", freq=12000.0)
    values, rate, _, _ = converted(tmp_path, source, target_rate=16000)

    assert rate == 16000
    assert level_at(values, 4000.0, rate) < -60.0


def test_downsampling_keeps_what_is_below_nyquist(tmp_path):
    """The filter must not eat the signal it is there to protect.

    A cut steep enough to stop aliasing can also dull everything below it, so
    pin the passband: 1 kHz is well inside 8 kHz and should survive at close to
    the level it went in at.
    """
    source = tone(tmp_path / "in.wav", freq=1000.0, amplitude=20000)
    values, rate, _, _ = converted(tmp_path, source, target_rate=16000)

    expected = 20 * math.log10(20000 / 32768.0)
    assert abs(level_at(values, 1000.0, rate) - expected) < 1.0


def test_output_matches_the_mixer_format(tmp_path):
    """audiomixer.Mixer rejects any sample whose format is not exactly this."""
    source = tone(tmp_path / "in.wav", freq=440.0, channels=2)
    _, rate, channels, width = converted(tmp_path, source, target_rate=16000)

    assert (rate, channels, width) == (16000, 1, 2)


def test_same_rate_is_left_alone(tmp_path):
    """A file already at the target rate should not be filtered on the way through."""
    source = tone(tmp_path / "in.wav", freq=1000.0, rate=16000)
    after, _, _, _ = converted(tmp_path, source, target_rate=16000)

    before, _, _, _ = read(source)
    assert after == before


def test_upsampling_does_not_invent_high_frequencies(tmp_path):
    """Going up in rate adds samples, never content.

    The band above the *source* Nyquist has to stay empty; an interpolator that
    reaches for it produces images of the original tone.
    """
    source = tone(tmp_path / "in.wav", freq=3000.0, rate=8000)
    values, rate, _, _ = converted(tmp_path, source, target_rate=16000)

    assert level_at(values, 3000.0, rate) > -20.0
    assert level_at(values, 5000.0, rate) < -60.0


def test_an_empty_file_converts_to_an_empty_file(tmp_path):
    """A 0-frame WAV is a thing a sample library contains, not an error."""
    source = pcm(tmp_path / "in.wav", [])
    values, rate, _, _ = converted(tmp_path, source, target_rate=16000)

    assert values == []
    assert rate == 16000


def test_one_frame_is_less_than_one_output_frame(tmp_path):
    """Decimating a single sample rounds the output length down to nothing.

    Worth pinning because it is the only route to the `count <= 0` branch, and
    because returning an empty list here is what keeps the loop below from
    running with no taps at all.
    """
    source = pcm(tmp_path / "in.wav", [12345])
    values, _, _, _ = converted(tmp_path, source, target_rate=16000)

    assert values == []


def test_one_frame_upsampled_keeps_its_level(tmp_path):
    """The narrowest tap range there is: one input sample, reached from two sides.

    Every output sample here is built from that single tap, so the result is
    only right if the kernel is normalised by the weights actually used rather
    than by a precomputed sum.
    """
    source = pcm(tmp_path / "in.wav", [12345], rate=8000)
    values, _, _, _ = converted(tmp_path, source, target_rate=16000)

    assert values == [12345, 12345]


@pytest.mark.parametrize("freq", (500.0, 2000.0, 6000.0))
def test_normalise_lifts_the_peak_without_clipping(tmp_path, freq):
    """Peak scaling has to survive the resampler's own overshoot.

    normalise() is pure peak-scaling and does not care about frequency, but
    what it is handed does: a windowed sinc rings, and the ringing grows as the
    content approaches the new Nyquist. These three walk from well inside the
    band up to 6 kHz against an 8 kHz Nyquist, which is where overshoot could
    push the scaled peak past full scale and wrap.
    """
    source = tone(tmp_path / "in.wav", freq=freq, amplitude=3000)
    values, _, _, _ = converted(tmp_path, source, target_rate=16000, do_normalise=True)

    peak = max(max(values), -min(values))
    assert 32767 * 0.90 < peak <= 32767


# --- trimming to fit the badge's RAM ---------------------------------------
#
# The firmware holds every sample in RAM and streams nothing, so length is a
# memory budget. Trimming here rather than at load is what lets the fade be
# chosen once, by ear, instead of by whichever track happened to load first.


def test_max_seconds_cuts_the_sample(tmp_path):
    source = tone(tmp_path / "long.wav", 440, rate=16000, seconds=2.0)
    out = str(tmp_path / "short.wav")
    convert_samples.convert(source, out, target_rate=16000, max_seconds=0.5)
    with wave.open(out, "rb") as result:
        assert result.getnframes() == 8000


def test_a_cut_sample_ends_in_silence(tmp_path):
    """A cut mid-waveform is a full-scale step, and a step is a click."""
    source = tone(tmp_path / "loud.wav", 440, rate=16000, seconds=2.0)
    out = str(tmp_path / "faded.wav")
    convert_samples.convert(source, out, target_rate=16000, max_seconds=0.5)
    with wave.open(out, "rb") as result:
        count = result.getnframes()
        values = struct.unpack("<%dh" % count, result.readframes(count))
    assert values[-1] == 0
    fade = int(16000 * convert_samples.FADE_MS / 1000.0)
    # The body is untouched: the loudest frame before the fade is still full.
    assert max(abs(v) for v in values[: count - fade]) > 19000
    # And the fade really descends rather than just ending on a zero crossing.
    tail = [abs(v) for v in values[count - fade :]]
    assert max(tail[: fade // 4]) > max(tail[-fade // 4 :])


def test_a_sample_shorter_than_the_limit_is_untouched(tmp_path):
    source = tone(tmp_path / "brief.wav", 440, rate=16000, seconds=0.2)
    out = str(tmp_path / "brief-out.wav")
    convert_samples.convert(source, out, target_rate=16000, max_seconds=1.0)
    with wave.open(out, "rb") as result:
        count = result.getnframes()
        values = struct.unpack("<%dh" % count, result.readframes(count))
    assert count == 3200
    assert values[-1] != 0, "an uncut sample must not be faded"


def test_no_limit_leaves_the_length_alone(tmp_path):
    source = tone(tmp_path / "full.wav", 440, rate=16000, seconds=1.0)
    out = str(tmp_path / "full-out.wav")
    convert_samples.convert(source, out, target_rate=16000)
    with wave.open(out, "rb") as result:
        assert result.getnframes() == 16000


def test_the_cut_happens_before_normalising(tmp_path):
    """Otherwise a peak in the discarded tail sets the gain for what is kept."""
    rate = 16000
    frames = [3000] * rate + [30000] * rate  # quiet half, then a loud tail
    source = pcm(tmp_path / "tail.wav", frames, rate=rate)
    out = str(tmp_path / "tail-out.wav")
    convert_samples.convert(
        source, out, target_rate=rate, do_normalise=True, max_seconds=1.0
    )
    with wave.open(out, "rb") as result:
        count = result.getnframes()
        values = struct.unpack("<%dh" % count, result.readframes(count))
    assert max(values) > 30000, "the kept half was not lifted to full scale"


# --- dead air on the end ---------------------------------------------------
#
# Every track shares 32 KB, so a tenth of a second of silence on the end of a
# snare is a tenth of a second another track does not get.


def test_trailing_silence_is_removed(tmp_path):
    rate = 16000
    frames = [12000] * rate + [0] * rate  # a second of sound, a second of air
    source = pcm(tmp_path / "air.wav", frames, rate=rate)
    out = str(tmp_path / "air-out.wav")
    convert_samples.convert(source, out, target_rate=rate)
    with wave.open(out, "rb") as result:
        assert abs(result.getnframes() - rate) < 50, "the silence stayed"


def test_quiet_is_not_the_same_as_silence(tmp_path):
    """A decaying tail must survive; only what is under the floor goes."""
    rate = 16000
    floor = convert_samples.silence_floor()
    frames = [12000] * 1000 + [floor * 4] * 1000
    source = pcm(tmp_path / "tail.wav", frames, rate=rate)
    out = str(tmp_path / "tail-out.wav")
    convert_samples.convert(source, out, target_rate=rate)
    with wave.open(out, "rb") as result:
        assert result.getnframes() > 1900, "the quiet tail was eaten"


def test_silence_in_the_middle_is_left_alone(tmp_path):
    """Only an unbroken run reaching the last frame counts as the end."""
    rate = 16000
    frames = [12000] * 500 + [0] * 500 + [12000] * 500
    source = pcm(tmp_path / "gap.wav", frames, rate=rate)
    out = str(tmp_path / "gap-out.wav")
    convert_samples.convert(source, out, target_rate=rate)
    with wave.open(out, "rb") as result:
        assert result.getnframes() >= 1400, "the gap in the middle was closed"


def test_trimming_silence_can_be_turned_off(tmp_path):
    rate = 16000
    frames = [12000] * 1000 + [0] * 1000
    source = pcm(tmp_path / "keep.wav", frames, rate=rate)
    out = str(tmp_path / "keep-out.wav")
    convert_samples.convert(source, out, target_rate=rate, trim=False)
    with wave.open(out, "rb") as result:
        assert result.getnframes() == 2000


def test_an_entirely_silent_sample_does_not_become_negative(tmp_path):
    source = pcm(tmp_path / "dead.wav", [0] * 1000, rate=16000)
    out = str(tmp_path / "dead-out.wav")
    convert_samples.convert(source, out, target_rate=16000)
    with wave.open(out, "rb") as result:
        assert result.getnframes() == 0


def test_silence_is_trimmed_before_the_length_limit(tmp_path):
    """Otherwise a limit is spent on air that was going to be dropped."""
    rate = 16000
    frames = [12000] * rate + [0] * rate
    source = pcm(tmp_path / "both.wav", frames, rate=rate)
    out = str(tmp_path / "both-out.wav")
    convert_samples.convert(source, out, target_rate=rate, max_seconds=0.5)
    with wave.open(out, "rb") as result:
        count = result.getnframes()
        values = struct.unpack("<%dh" % count, result.readframes(count))
    assert count == 8000
    # Half a second of actual sound, not a quarter of sound and a quarter of air.
    assert max(abs(v) for v in values[:7000]) > 10000
