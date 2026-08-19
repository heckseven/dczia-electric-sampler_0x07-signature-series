"""Tests for WAV header reading.

engine.wav imports nothing from CircuitPython, so these run directly.
"""

import io
import struct

import pytest

from engine.wav import WavError, describe, matches, read_format


def build_wav(rate=22050, channels=1, bits=16, frames=32, extra_chunk=False):
    data = struct.pack("<%dh" % frames, *([0] * frames))
    fmt = struct.pack(
        "<HHIIHH",
        1,
        channels,
        rate,
        rate * channels * bits // 8,
        channels * bits // 8,
        bits,
    )
    body = b"WAVE"
    if extra_chunk:
        # A LIST chunk before data, as many editors write.
        body += b"LIST" + struct.pack("<I", 4) + b"INFO"
    body += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_reads_a_plain_wav():
    rate, channels, bits, offset, size = read_format(io.BytesIO(build_wav()))
    assert (rate, channels, bits) == (22050, 1, 16)
    assert size == 64


def test_data_offset_points_at_the_audio():
    raw = build_wav(frames=16)
    rate, channels, bits, offset, size = read_format(io.BytesIO(raw))
    assert raw[offset : offset + size] == raw[-size:]


def test_skips_chunks_before_data():
    """Editors commonly write LIST or fact chunks ahead of the audio."""
    rate, channels, bits, offset, size = read_format(
        io.BytesIO(build_wav(extra_chunk=True))
    )
    assert (rate, channels, bits) == (22050, 1, 16)
    assert size == 64


@pytest.mark.parametrize(
    "rate,channels,bits", [(44100, 1, 16), (22050, 2, 16), (22050, 1, 8)]
)
def test_reports_formats_that_do_not_match(rate, channels, bits):
    got = read_format(io.BytesIO(build_wav(rate, channels, bits)))
    assert not matches(got[0], got[1], got[2], 22050, 1, 16)


def test_a_matching_format_is_accepted():
    got = read_format(io.BytesIO(build_wav()))
    assert matches(got[0], got[1], got[2], 22050, 1, 16)


def test_rejects_a_non_riff_file():
    with pytest.raises(WavError):
        read_format(io.BytesIO(b"this is not a wav file at all"))


def test_rejects_an_empty_file():
    with pytest.raises(WavError):
        read_format(io.BytesIO(b""))


def test_rejects_a_truncated_header():
    with pytest.raises(WavError):
        read_format(io.BytesIO(build_wav()[:8]))


def test_rejects_non_pcm_encoding():
    raw = bytearray(build_wav())
    fmt_at = raw.find(b"fmt ") + 8
    raw[fmt_at : fmt_at + 2] = struct.pack("<H", 3)  # IEEE float
    with pytest.raises(WavError):
        read_format(io.BytesIO(bytes(raw)))


def test_rejects_a_file_with_no_data_chunk():
    raw = build_wav()
    with pytest.raises(WavError):
        read_format(io.BytesIO(raw.replace(b"data", b"junk")))


def test_describe_is_readable():
    assert describe(22050, 1, 16) == "22050 Hz mono 16-bit"
    assert describe(44100, 2, 16) == "44100 Hz 2ch 16-bit"
