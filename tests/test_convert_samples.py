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
        return values, source.getframerate(), source.getnchannels(), source.getsampwidth()


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
