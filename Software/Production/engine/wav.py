"""Reading WAV headers, so the sampler can decide how to play a file.

The badge streams audio straight off storage unless told otherwise, and that
is where playback quality is won or lost. Measured on the hardware:

    audio needs           43.1 KB/s per playing voice at 22050/mono/16
    onboard flash gives  391 KB/s
    SD card gives        169 KB/s in small reads, 412 KB/s in large ones

Three voices streaming from a card is already close to the limit, and eight
tracks would need 345 KB/s, which no small read pattern on SD sustains. The
symptom is a starved I2S buffer, which sounds like harsh digital distortion
rather than a dropout. Loading short samples into RAM instead takes storage
out of the audio path entirely, and drum one-shots are small enough that this
is usually possible.

Deciding needs the header: how big the audio is, and whether its format
matches the mixer at all. This module does that and nothing else, so it is
pure logic and tested directly.
"""

import struct


class WavError(ValueError):
    """The file is not a WAV this badge can play."""


def read_format(fileobj, max_scan=64):
    """Return (rate, channels, bits, data_offset, data_size).

    Reads only the header chunks, leaving the file positioned arbitrarily.
    Raises WavError on anything that is not a PCM RIFF/WAVE file.
    """
    header = fileobj.read(12)
    if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise WavError("not a RIFF/WAVE file")

    rate = channels = bits = None
    offset = 12
    for _ in range(max_scan):
        chunk = fileobj.read(8)
        if len(chunk) < 8:
            break
        chunk_id = chunk[0:4]
        size = struct.unpack("<I", chunk[4:8])[0]
        offset += 8
        if chunk_id == b"fmt ":
            body = fileobj.read(size)
            if len(body) < 16:
                raise WavError("truncated fmt chunk")
            encoding, channels, rate, _byte_rate, _align, bits = struct.unpack(
                "<HHIIHH", body[0:16]
            )
            if encoding != 1:
                raise WavError("not PCM")
            offset += size
        elif chunk_id == b"data":
            if rate is None:
                raise WavError("data before fmt")
            return rate, channels, bits, offset, size
        else:
            fileobj.seek(offset + size)
            offset += size
        # Chunks are word aligned; an odd size carries a pad byte.
        if size % 2:
            fileobj.seek(offset + 1)
            offset += 1
    raise WavError("no data chunk")


def matches(rate, channels, bits, want_rate, want_channels, want_bits):
    return rate == want_rate and channels == want_channels and bits == want_bits


def describe(rate, channels, bits):
    return "%d Hz %s %d-bit" % (
        rate,
        "mono" if channels == 1 else "%dch" % channels,
        bits,
    )
