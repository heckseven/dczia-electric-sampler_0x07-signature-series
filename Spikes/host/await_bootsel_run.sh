#!/usr/bin/env bash
# Wait for someone to hold BOOTSEL while replugging, then take it from there.
#
# This exists because that button press is the one step the harness cannot do.
# Everything after it is automatic, so the person pressing it does not also have
# to come back and start something.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DEADLINE=$(( $(date +%s) + 3600 ))

echo "waiting for RPI-RP2 (hold BOOTSEL while replugging)"
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    dev=$(lsblk -o NAME,LABEL -nr | awk '$2=="RPI-RP2"{print "/dev/"$1; exit}')
    if [ -n "$dev" ]; then
        echo "BOOTSEL: $dev"
        mnt=$(lsblk -o MOUNTPOINT -nr "$dev" | head -1)
        if [ -z "$mnt" ]; then
            timeout 20 udisksctl mount -b "$dev" >/dev/null 2>&1
            sleep 2
            mnt=$(lsblk -o MOUNTPOINT -nr "$dev" | head -1)
        fi
        [ -n "$mnt" ] || { echo "could not mount $dev"; exit 1; }
        echo "mounted at $mnt"
        cp "$HERE/../c/build/spike_mixer.uf2" "$mnt/" 2>/dev/null
        sync 2>/dev/null
        echo "flashed spike_mixer v4; waiting for it to come back"
        sleep 6
        timeout 120 python3 "$HERE/read_c_spike.py"
        exit $?
    fi
    sleep 2
done
echo "nobody pressed BOOTSEL within the hour"
exit 1
