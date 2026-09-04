"""Read glyphs out of an X11 PCF bitmap font.

PCF is what the classic hand-drawn pixel fonts ship as - 4x6, 5x7, 5x8, 6x9 -
and nothing on this machine converts it. The format is documented and small, so
it is parsed here rather than worked around: a font somebody drew pixel by pixel
is worth more at this size than any scalable face rendered grey and thresholded,
which is how the first attempt produced an M indistinguishable from a block.

Only the three tables a glyph needs: what the bitmaps are, how big each is, and
which character each belongs to.
"""

import gzip
import struct

PCF_METRICS = 0x00000004
PCF_BITMAPS = 0x00000008
PCF_BDF_ENCODINGS = 0x00000020

FORMAT_COMPRESSED_METRICS = 0x00000100
FORMAT_BYTE_MSB = 0x00000004
FORMAT_BIT_MSB = 0x00000008


class Reader:
    """Endianness in PCF is per table, carried in that table's format word."""

    def __init__(self, data, offset, big):
        self.data = data
        self.at = offset
        self.big = big

    def u32(self):
        v = struct.unpack_from(">I" if self.big else "<I", self.data, self.at)[0]
        self.at += 4
        return v

    def i16(self):
        v = struct.unpack_from(">h" if self.big else "<h", self.data, self.at)[0]
        self.at += 2
        return v

    def u8(self):
        v = self.data[self.at]
        self.at += 1
        return v


def load(path):
    data = gzip.open(path, "rb").read() if path.endswith(".gz") else open(path, "rb").read()
    if data[:4] != b"\x01fcp":
        raise ValueError("not a PCF file")

    count = struct.unpack_from("<I", data, 4)[0]
    tables = {}
    for i in range(count):
        kind, fmt, size, offset = struct.unpack_from("<4I", data, 8 + i * 16)
        tables[kind] = (fmt, size, offset)

    bitmaps = read_bitmaps(data, tables[PCF_BITMAPS])
    metrics = read_metrics(data, tables[PCF_METRICS])
    encodings = read_encodings(data, tables[PCF_BDF_ENCODINGS])
    return bitmaps, metrics, encodings


def read_bitmaps(data, table):
    _, _, offset = table
    fmt = struct.unpack_from("<I", data, offset)[0]
    r = Reader(data, offset + 4, bool(fmt & FORMAT_BYTE_MSB))
    n = r.u32()
    offsets = [r.u32() for _ in range(n)]
    sizes = [r.u32() for _ in range(4)]
    padding = 1 << (fmt & 3)
    base = r.at
    blob = data[base:base + sizes[fmt & 3]]
    return {
        "offsets": offsets,
        "data": blob,
        "padding": padding,
        "bit_msb": bool(fmt & FORMAT_BIT_MSB),
    }


def read_metrics(data, table):
    _, _, offset = table
    fmt = struct.unpack_from("<I", data, offset)[0]
    big = bool(fmt & FORMAT_BYTE_MSB)
    r = Reader(data, offset + 4, big)
    out = []
    if fmt & FORMAT_COMPRESSED_METRICS:
        n = r.i16()
        for _ in range(n):
            left = r.u8() - 128
            right = r.u8() - 128
            width = r.u8() - 128
            ascent = r.u8() - 128
            descent = r.u8() - 128
            out.append((left, right, width, ascent, descent))
    else:
        n = r.u32()
        for _ in range(n):
            left, right, width, ascent, descent = (r.i16() for _ in range(5))
            r.i16()  # attributes
            out.append((left, right, width, ascent, descent))
    return out


def read_encodings(data, table):
    _, _, offset = table
    fmt = struct.unpack_from("<I", data, offset)[0]
    r = Reader(data, offset + 4, bool(fmt & FORMAT_BYTE_MSB))
    min2, max2, min1, max1, _default = (r.i16() for _ in range(5))
    mapping = {}
    for byte1 in range(min1, max1 + 1):
        for byte2 in range(min2, max2 + 1):
            index = r.i16()
            if index != 0xFFFF and index >= 0:
                code = byte2 if min1 == max1 == 0 else (byte1 << 8) | byte2
                mapping[code] = index
    return mapping


def glyph(bitmaps, metrics, index):
    """One glyph as a list of rows of 0/1, plus its metrics."""
    left, right, width, ascent, descent = metrics[index]
    w = right - left
    h = ascent + descent
    start = bitmaps["offsets"][index]
    row_bytes = ((w + bitmaps["padding"] * 8 - 1) //
                 (bitmaps["padding"] * 8)) * bitmaps["padding"]

    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            byte = bitmaps["data"][start + y * row_bytes + (x >> 3)]
            bit = (byte >> (7 - (x & 7))) & 1 if bitmaps["bit_msb"] \
                else (byte >> (x & 7)) & 1
            row.append(bit)
        rows.append(row)
    return rows, (left, right, width, ascent, descent)
