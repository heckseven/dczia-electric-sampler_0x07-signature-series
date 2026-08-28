#!/usr/bin/env python3
"""Convert WAV files to the format the badge's audio mixer expects.

The sampler builds its audiomixer.Mixer with a fixed format, so every sample it
plays has to match it exactly:

    16000 Hz, 1 channel (mono), 16-bit signed PCM by default

A file at any other rate or channel count either plays at the wrong pitch or is
rejected outright. The 909 pack under "Samples/909 Samples" ships as 44100 Hz
stereo, which is why those files cannot be dropped straight into /samples/.

Usage:
    python3 Tools/convert_samples.py INPUT [INPUT ...] -o OUTPUT_DIR

Example:
    python3 Tools/convert_samples.py "Samples/909 Samples"/*.wav -o /tmp/909

The rate is settable with --rate, because the firmware's mixer rate is a
tunable trade: a lower rate costs bandwidth but leaves far more room for
voices and for holding samples in RAM.

Only the Python standard library is used, so this runs anywhere without a
virtualenv.
"""

import argparse
import math
import os
import struct
import sys
import wave

DEFAULT_RATE = 16000
TARGET_CHANNELS = 1
TARGET_WIDTH = 2  # bytes per sample, i.e. 16-bit


def read_frames(path):
    """Return (samples, rate) with samples as a flat list of per-channel ints."""
    with wave.open(path, "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        raw = source.readframes(source.getnframes())

    if width == 1:
        # 8-bit WAV is unsigned; centre it and scale up to 16-bit.
        values = [(byte - 128) * 256 for byte in raw]
    elif width == 2:
        values = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    elif width == 4:
        values = [v >> 16 for v in struct.unpack("<%di" % (len(raw) // 4), raw)]
    else:
        raise ValueError("unsupported sample width: %d bytes" % width)

    return values, rate, channels


def to_mono(values, channels):
    """Average interleaved channels down to one."""
    if channels == 1:
        return values
    mono = []
    for index in range(0, len(values) - channels + 1, channels):
        mono.append(sum(values[index : index + channels]) // channels)
    return mono


# How far the resampling filter reaches, either side of the output sample being
# computed - in output samples when decimating, and in source samples when
# interpolating, where the kernel has to stay wide rather than narrow with the
# ratio.
#
# Wider is a steeper cut and proportionally more arithmetic. Measured at 16 for
# 44100 -> 16000, by converting tones from above the new 8 kHz Nyquist and
# reading back the frequency each one folds to:
#
#     9 kHz  -> 7 kHz     -43.6 dB
#    10 kHz  -> 6 kHz     -80.4 dB
#    12 kHz  -> 4 kHz     -90.3 dB   (ffmpeg's swresample: -91.5 dB)
#    15 kHz  -> 1 kHz     -97.6 dB
#    20 kHz  -> 4 kHz    -109.4 dB
#
# So the cut is not a brick wall: the first kilohertz above Nyquist survives
# about 44 dB down and everything past it is gone. That is the ordinary
# tradeoff for a filter this length, and 44 dB of a band the speaker barely
# reproduces is not worth doubling the arithmetic for.
#
# Cost is roughly half of real time - a 2.06 s cymbal took 0.96 s - so the
# 98-sample set here, 150 seconds of audio, is a bit over a minute.
FILTER_HALF_WIDTH = 16


def _sinc(x):
    if x == 0.0:
        return 1.0
    x *= math.pi
    return math.sin(x) / x


def _blackman(offset, reach):
    """Blackman window over [-reach, reach], zero outside.

    The caller's tap range already keeps offset inside the window, so this
    bound is float safety rather than logic: reach is not a whole number when
    decimating, and a tap can round onto the edge exactly.
    """
    if abs(offset) >= reach:
        return 0.0
    angle = math.pi * offset / reach
    return 0.42 + 0.5 * math.cos(angle) + 0.08 * math.cos(2.0 * angle)


def resample(values, source_rate, target_rate):
    """Band-limited resample to the target rate.

    Interpolating between neighbouring samples is not enough when the rate goes
    down. 44100 -> 16000 throws away nearly two thirds of the samples, and
    everything above the new 8 kHz Nyquist does not disappear: it folds back
    into the audible band. Measured on a 12 kHz tone, plain linear
    interpolation returned it as a 4 kHz tone only 6.5 dB below the original,
    so cymbals and open hats - which are mostly content above 8 kHz - came out
    as a wash of frequencies that were never in the source.

    The kernel is a windowed sinc, cut at whichever of the two Nyquists is
    lower, so it low-passes on the way down and interpolates on the way up.
    Evaluating it only where an output sample lands makes the filtering and the
    rate change one pass, and costs time proportional to the output length
    rather than the input's.
    """
    if source_rate == target_rate or not values:
        return values
    ratio = source_rate / target_rate
    count = int(len(values) / ratio)
    if count <= 0:
        return []

    # Both expressed in source samples: the cutoff as a fraction of the source
    # rate, the reach widened when decimating because one output sample then
    # has to gather that many more input ones.
    cutoff = 0.5 / ratio if ratio > 1.0 else 0.5
    reach = FILTER_HALF_WIDTH * max(1.0, ratio)
    last = len(values) - 1

    out = []
    for index in range(count):
        centre = index * ratio
        first = max(0, int(math.ceil(centre - reach)))
        stop = min(last, int(math.floor(centre + reach)))
        total = 0.0
        weights = 0.0
        for tap in range(first, stop + 1):
            offset = tap - centre
            weight = _blackman(offset, reach) * _sinc(2.0 * cutoff * offset)
            total += values[tap] * weight
            weights += weight
        # Normalising by the weights actually used keeps the level right at the
        # very start and end, where the kernel runs off the end of the sample.
        # The sum cannot reach zero in practice - the tap nearest the centre is
        # always within half a sample of it, where the kernel is at its peak -
        # but a rate pair nobody has tried should not divide by zero.
        out.append(int(round(total / weights)) if weights else 0)
    return out


def clamp(values):
    return [max(-32768, min(32767, value)) for value in values]


NORMALISE_PEAK = 0.97


def normalise(values, target=NORMALISE_PEAK):
    """Scale so the loudest sample sits just below full scale.

    Worth doing: the samples this project shipped peak at 48%, 32% and 26% of
    full scale, which throws away 6 to 12 dB on a badge with a very small
    speaker. Gain applied here costs nothing, whereas gain applied at the mixer
    eats the headroom two overlapping voices need.
    """
    peak = max(max(values), -min(values)) if values else 0
    if peak <= 0:
        return values
    gain = (32767 * target) / peak
    return [int(value * gain) for value in values]


def convert(in_path, out_path, target_rate=DEFAULT_RATE, do_normalise=False):
    values, rate, channels = read_frames(in_path)
    values = to_mono(values, channels)
    values = resample(values, rate, target_rate)
    if do_normalise:
        values = normalise(values)
    values = clamp(values)

    with wave.open(out_path, "wb") as target:
        target.setnchannels(TARGET_CHANNELS)
        target.setsampwidth(TARGET_WIDTH)
        target.setframerate(target_rate)
        target.writeframes(struct.pack("<%dh" % len(values), *values))

    return rate, channels, len(values)


def main():
    parser = argparse.ArgumentParser(
        description="Convert WAV files to 22050 Hz mono 16-bit for the badge."
    )
    parser.add_argument("inputs", nargs="+", help="WAV files to convert")
    parser.add_argument(
        "-o", "--output-dir", required=True, help="directory to write converted files"
    )
    parser.add_argument(
        "-n",
        "--normalise",
        action="store_true",
        help="scale each sample so its peak sits just below full scale",
    )
    parser.add_argument(
        "-r",
        "--rate",
        type=int,
        default=DEFAULT_RATE,
        help="target sample rate (default %d, must match the firmware's mixer)"
        % DEFAULT_RATE,
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    failures = 0
    for in_path in args.inputs:
        name = os.path.basename(in_path)
        out_path = os.path.join(args.output_dir, name)
        try:
            rate, channels, frames = convert(
                in_path, out_path, args.rate, args.normalise
            )
        except (OSError, ValueError, wave.Error) as error:
            print("SKIP %s: %s" % (name, error), file=sys.stderr)
            failures += 1
            continue
        layout = "mono" if channels == 1 else "%dch" % channels
        print(
            "%-28s %5d Hz %-5s -> %d Hz mono, %d frames"
            % (name, rate, layout, args.rate, frames)
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
