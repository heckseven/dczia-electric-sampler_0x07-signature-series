"""Task 3: what the current firmware actually does, measured by itself.

Deployed alongside the firmware and run from the REPL, rather than replacing
code.py - the point is to measure the shipping engine, so this imports it and
drives it exactly as main.py does.

Everything prints in the Spikes report format, one record per line, so a spike
that dies mid-run still yields the cases that finished. Cases announce
themselves before doing anything; a `CASE ... state=STARTED` with no matching
`RESULT` is the case that killed the badge.

What this establishes, and why each one is here:

  timebase     Every other number in this file is quoted in nanoseconds from
               time.monotonic_ns(), which on this port is a 1024 Hz tick plus
               subticks and is nothing like nanosecond-resolution. Measure the
               real granularity first or every figure below is precise-looking
               and wrong.

  constants    The scan interval, debounce threshold and mixer buffer depth
               read back from the live objects. The badge runs a DCZia-supplied
               .uf2 and nothing records whether it is stock CircuitPython, so
               reading upstream source proves what upstream does, not what is
               on this chip.

  loop         The main loop's pass duration. This gates how fast anything can
               be serviced, and the repo's own numbers disagree by a factor of
               thirty-four - sequencer.py says "around 200 us", the handoff
               measures ~125 passes/s, the root-cause doc measures 6.8 ms.
               One of those is right.

  gc           Collection count, pause duration and the free-memory floor,
               under load rather than idle. A collection is 25-27 ms against a
               32 ms audio buffer; on an idle badge the tail is fiction.

Still to come, and named here so it is not forgotten: the mixer's buffer_size
swept downward. Nothing passes one, so it takes the 1024-byte default - 32 ms.
Enlarging it was refused; nobody has tried the other direction, and that curve
is what the rewrite's block size gets written against. It needs the audio path
rebuilt underneath a running engine, which is worth doing carefully rather than
first.

The load is pinned for every timed case: transport running, a four-track
pattern, the display refreshing and the LEDs animating. Measuring an idle badge
would answer a question nobody asked.
"""

import gc

# The firmware under test. Imported, not reimplemented.
import sequencer as sequencer_module
from sequencer import engine

REPORT_VERSION = 1

# How many passes to time. At a few thousand passes a second this is a second
# or two of loop, which is long enough for a collection to land inside it and
# short enough that the histogram fits in memory.
LOOP_PASSES = 4000

# Bucket edges in microseconds. Chosen to straddle every figure the repo
# disagrees about - 200 us, 6.8 ms, 8 ms - and to put the audio buffer's 32 ms
# and a collection's 25-27 ms in their own buckets.
BUCKETS_US = (100, 200, 500, 1000, 2000, 5000, 10000, 20000, 30000, 50000)


def _emit(keyword, **fields):
    """One record. Values must not contain spaces; none of ours do."""
    parts = [keyword]
    for key in sorted(fields):
        parts.append("%s=%s" % (key, fields[key]))
    print(" ".join(parts))


def _start(case):
    _emit("CASE", case=case, state="STARTED")


def _monotonic_ns():
    """time.monotonic_ns if this build has it, else None.

    Not assumed present: it is absent on some builds, and a spike that dies on
    an import is a spike that measures nothing.
    """
    try:
        from time import monotonic_ns

        return monotonic_ns
    except ImportError:
        return None


def case_timebase():
    """What resolution the clock actually has, before anything is timed with it."""
    _start("timebase")
    monotonic_ns = _monotonic_ns()
    if monotonic_ns is None:
        _emit("RESULT", case="timebase", available=0)
        return None

    # The smallest non-zero step the clock reports, sampled by spinning until
    # it moves. Repeated, because the first transition after a tick boundary is
    # not representative of the rest.
    steps = []
    for _ in range(64):
        first = monotonic_ns()
        while True:
            second = monotonic_ns()
            if second != first:
                break
        steps.append(second - first)

    backwards = 0
    previous = monotonic_ns()
    for _ in range(2000):
        now = monotonic_ns()
        if now < previous:
            backwards += 1
        previous = now

    _emit(
        "RESULT",
        case="timebase",
        available=1,
        min_step_ns=min(steps),
        max_step_ns=max(steps),
        median_step_ns=sorted(steps)[len(steps) // 2],
        backwards=backwards,
    )
    return monotonic_ns


def case_constants():
    """Read the numbers off the live objects rather than trusting the source."""
    _start("constants")
    from setup import keys

    fields = {"case": "constants"}

    # keypad.KeyMatrix does not expose its interval, so this reports what can
    # actually be read and leaves the rest to be inferred from the loop timing.
    for name in ("key_count", "interval", "debounce_threshold"):
        try:
            fields[name] = getattr(keys, name)
        except AttributeError:
            fields[name] = "unreadable"

    mixer = engine.mixer
    for name, source in (
        ("mixer_voices", lambda: len(mixer.voice)),
        ("mixer_rate", lambda: sequencer_module.SAMPLE_RATE),
        ("ram_budget", lambda: sequencer_module.RAM_BUDGET),
        ("ram_used", lambda: engine.ram_used),
        ("midi_poll_ms", lambda: sequencer_module.MIDI_POLL_MS),
    ):
        try:
            fields[name] = source()
        except Exception:
            fields[name] = "unreadable"

    _emit("RESULT", **fields)


def _pin_load():
    """The condition every timed case runs under.

    Transport playing a four-track pattern. The display and LEDs are driven by
    whatever state is on screen, which the caller sets up; what matters here is
    that the engine is not idle, because an idle badge collects rarely and
    reports a tail that does not exist in use.
    """
    engine.load_demo_pattern()
    if not engine.transport.playing:
        engine.toggle_play()


def _histogram(samples_us):
    """Bucket counts plus the order statistics that actually get quoted."""
    counts = [0] * (len(BUCKETS_US) + 1)
    for value in samples_us:
        placed = False
        for index in range(len(BUCKETS_US)):
            if value < BUCKETS_US[index]:
                counts[index] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    ordered = sorted(samples_us)
    count = len(ordered)
    return counts, {
        "min": ordered[0],
        "p50": ordered[count // 2],
        "p99": ordered[min(count - 1, (count * 99) // 100)],
        "max": ordered[-1],
    }


def case_loop(monotonic_ns, machine=None, passes=LOOP_PASSES):
    """The main loop's pass duration, under load.

    main.py cannot be imported - its event loop runs at module scope - so its
    body is reproduced here and has to be kept in step with it. What matters
    for the timing is the order: feed the watchdog, check the GC floor, tick
    the engine, update whatever is on screen.
    """
    _start("loop")
    if monotonic_ns is None:
        _emit("RESULT", case="loop", available=0)
        return

    import guard

    gc_floor = 16 * 1024
    samples = []
    collects = 0
    free_floor = gc.mem_free()

    _pin_load()
    gc.collect()

    for _ in range(passes):
        started = monotonic_ns()
        guard.feed()
        free = gc.mem_free()
        if free < free_floor:
            free_floor = free
        if free < gc_floor:
            gc.collect()
            collects += 1
        engine.tick()
        if machine is not None:
            machine.update()
        samples.append((monotonic_ns() - started) // 1000)

    counts, stats = _histogram(samples)
    _emit(
        "RESULT",
        case="loop",
        available=1,
        passes=passes,
        collects=collects,
        free_floor=free_floor,
        buckets_us=",".join(str(b) for b in BUCKETS_US),
        counts=",".join(str(c) for c in counts),
        **stats
    )


def case_gc(monotonic_ns):
    """How long a collection takes, and how often one is needed under load."""
    _start("gc")
    if monotonic_ns is None:
        _emit("RESULT", case="gc", available=0)
        return

    _pin_load()
    pauses = []
    for _ in range(24):
        # Let the loop generate real garbage between collections rather than
        # timing a collect on an already-clean heap, which is the fast case and
        # not the one that interrupts audio.
        for _ in range(200):
            engine.tick()
        started = monotonic_ns()
        gc.collect()
        pauses.append((monotonic_ns() - started) // 1000)

    counts, stats = _histogram(pauses)
    _emit(
        "RESULT",
        case="gc",
        available=1,
        collections=len(pauses),
        free_after=gc.mem_free(),
        buckets_us=",".join(str(b) for b in BUCKETS_US),
        counts=",".join(str(c) for c in counts),
        **stats
    )


def run(machine=None):
    """Every case, in order, each announced before it runs."""
    _emit("SPIKE", name="baseline", version=REPORT_VERSION)
    monotonic_ns = case_timebase()
    case_constants()
    case_loop(monotonic_ns, machine)
    case_gc(monotonic_ns)
    _emit("DONE", spike="baseline")
