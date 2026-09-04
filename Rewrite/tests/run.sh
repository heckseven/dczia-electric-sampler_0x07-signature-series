#!/usr/bin/env bash
# Every host test, in one command.
#
# These run on the build machine because what they check is arithmetic and
# state, and arithmetic is better enumerated exhaustively in a millisecond than
# sampled on hardware. The badge is for what only the badge can answer -
# latency, jitter, underruns, and whether it sounds right.
set -u
cd "$(dirname "$0")/.."

CC=${CC:-gcc}
FLAGS="-std=c11 -Wall -Wextra -Iinclude"
fail=0

run() {
    local name=$1; shift
    if ! $CC $FLAGS -o "/tmp/rt_$name" "$@" 2>/tmp/rt_$name.build; then
        echo "BUILD FAILED $name"
        head -5 "/tmp/rt_$name.build"
        fail=1
        return
    fi
    if "/tmp/rt_$name"; then
        :
    else
        fail=1
    fi
}

run seq     tests/test_seq.c     src/song.c src/seq.c
run msgpack tests/test_msgpack.c src/msgpack.c
run menu    tests/test_menu.c    src/menu.c src/anim.c src/song.c
run songfile tests/test_songfile.c src/song.c src/songfile.c src/msgpack.c
run anim    tests/test_anim.c    src/anim.c
run midi    tests/test_midi.c    src/midi.c
run prefs   tests/test_prefs.c   src/prefs.c src/msgpack.c

exit $fail
