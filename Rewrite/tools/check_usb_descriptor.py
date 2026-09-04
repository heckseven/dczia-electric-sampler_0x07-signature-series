#!/usr/bin/env python3
"""Parse the USB configuration descriptor out of a built ELF and check it.

Written because a badge that will not enumerate cannot be asked what is wrong
with it. The only tools that talk to this board go over the very thing that is
broken, so the first question - "is the descriptor malformed, or is it
something else?" - has to be answerable from the image on disk.

It earned its place: when USB MIDI first failed to enumerate, this said the
descriptor was well formed, which ruled out the obvious suspect and sent the
search to where the fault actually was (nothing was calling tud_task).

Usage:  tools/check_usb_descriptor.py build/rt.elf
"""

import re
import subprocess
import sys

TOOLCHAIN = "/home/heckseven/toolchains/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin"

DESC_TYPES = {
    0x02: "CONFIG",
    0x04: "INTERFACE",
    0x05: "ENDPOINT",
    0x0B: "IAD",
    0x24: "CS_INTERFACE",
    0x25: "CS_ENDPOINT",
}


def tool(name):
    return f"{TOOLCHAIN}/arm-none-eabi-{name}"


def read_symbol(elf, symbol):
    """The bytes of one symbol, from the ELF's own dump of its sections."""
    nm = subprocess.run([tool("nm"), "-S", elf], capture_output=True, text=True).stdout
    rows = [line for line in nm.splitlines() if line.endswith(" " + symbol)]
    if not rows:
        raise SystemExit(f"{symbol} not found in {elf}")
    fields = rows[0].split()
    address, size = int(fields[0], 16), int(fields[1], 16)

    dump = subprocess.run([tool("objdump"), "-s", elf], capture_output=True, text=True).stdout
    data = {}
    for line in dump.splitlines():
        match = re.match(r"\s*([0-9a-f]{8})\s+((?:[0-9a-f]{2,8} ?)+?)\s\s", line)
        if match:
            base = int(match.group(1), 16)
            raw = match.group(2).replace(" ", "")
            for i in range(0, len(raw), 2):
                data[base + i // 2] = int(raw[i : i + 2], 16)
    return bytes(data.get(address + i, 0) for i in range(size))


def check(desc):
    """Walk the descriptor, print it, and return whether it holds together."""
    offset = 0
    interfaces, endpoints = set(), []
    total = None

    while offset < len(desc):
        length, kind = desc[offset], desc[offset + 1]
        if length == 0:
            print("  !! zero-length descriptor - the walk cannot continue")
            return False
        name = DESC_TYPES.get(kind, f"0x{kind:02x}")
        extra = ""
        if kind == 0x02:
            total = desc[offset + 2] | (desc[offset + 3] << 8)
            extra = f" wTotalLength={total} bNumInterfaces={desc[offset + 4]}"
        elif kind == 0x04:
            interfaces.add(desc[offset + 2])
            extra = (f" itf={desc[offset + 2]} alt={desc[offset + 3]}"
                     f" nEP={desc[offset + 4]} class=0x{desc[offset + 5]:02x}")
        elif kind == 0x05:
            endpoints.append(desc[offset + 2])
            extra = (f" addr=0x{desc[offset + 2]:02x}"
                     f" maxpacket={desc[offset + 4] | (desc[offset + 5] << 8)}")
        elif kind == 0x0B:
            extra = (f" firstItf={desc[offset + 2]} count={desc[offset + 3]}"
                     f" class=0x{desc[offset + 4]:02x}")
        print(f"  +{offset:3d} len={length:2d} {name}{extra}")
        offset += length

    print()
    ok = True

    if total != len(desc):
        print(f"FAIL wTotalLength says {total}, the symbol holds {len(desc)}")
        ok = False
    else:
        print(f"ok   wTotalLength {total} matches the bytes actually present")

    declared = desc[4]
    if sorted(interfaces) != list(range(declared)):
        print(f"FAIL interfaces {sorted(interfaces)} are not 0..{declared - 1}")
        ok = False
    else:
        print(f"ok   interfaces {sorted(interfaces)} are contiguous "
              f"and match bNumInterfaces={declared}")

    # A repeated endpoint address enumerates and then misbehaves under load,
    # which is far worse to diagnose than a device that fails outright.
    if len(endpoints) != len(set(endpoints)):
        print(f"FAIL an endpoint address is used twice: "
              f"{[hex(e) for e in endpoints]}")
        ok = False
    else:
        print(f"ok   endpoints {[hex(e) for e in endpoints]} are all distinct")

    return ok


def main():
    elf = sys.argv[1] if len(sys.argv) > 1 else "build/rt.elf"
    desc = read_symbol(elf, "usbd_desc_cfg")
    print(f"usbd_desc_cfg in {elf}: {len(desc)} bytes\n")
    ok = check(desc)
    print("\nDESCRIPTOR", "LOOKS WELL FORMED" if ok else "IS BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
