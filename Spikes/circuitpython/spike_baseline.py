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

# Timings are streamed into buckets rather than stored.
#
# The first version of this kept a list of 4,000 samples and died with
# "MemoryError: allocating 16384 bytes" the moment the real state machine was
# attached - a list of 4,000 ints doubles to 4,096 slots, which is 16 KB, and
# with the sampler screen and settings tree built there is no 16 KB. Measuring
# the loop must not be the thing that changes what the loop has to work with.
#
# Fine and uniform below 20 ms so percentiles are meaningful at 250 us - the
# loop turns out to live at 5-10 ms, and 5,000 us buckets cannot tell 6 ms from
# 9 ms. Coarse above, where only the shape of the tail matters.
FINE_US = 250
FINE_LIMIT_US = 20000
FINE_BUCKETS = FINE_LIMIT_US // FINE_US
COARSE_EDGES_US = (25000, 30000, 40000, 60000)


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


class Histogram:
    """Counts, min and max, without keeping the samples.

    Allocated once up front so that nothing grows while the thing being
    measured is running.
    """

    def __init__(self):
        self.counts = [0] * (FINE_BUCKETS + len(COARSE_EDGES_US) + 1)
        self.total = 0
        self.min = None
        self.max = None

    def add(self, value):
        self.total += 1
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value
        if value < FINE_LIMIT_US:
            # Uniform below the limit, so the bucket is arithmetic rather than
            # a scan - this runs once per loop pass.
            self.counts[value // FINE_US] += 1
            return
        for offset in range(len(COARSE_EDGES_US)):
            if value < COARSE_EDGES_US[offset]:
                self.counts[FINE_BUCKETS + offset] += 1
                return
        self.counts[-1] += 1

    def _upper(self, index):
        """The upper edge of a bucket, for reporting a percentile."""
        if index < FINE_BUCKETS:
            return (index + 1) * FINE_US
        offset = index - FINE_BUCKETS
        if offset < len(COARSE_EDGES_US):
            return COARSE_EDGES_US[offset]
        return -1  # overflow: above the last edge, no upper bound to quote

    def percentile(self, fraction):
        """Bucket-resolution percentile: the upper edge of the bucket it lands in.

        Quoted as an upper bound rather than an interpolation, because 250 us
        of honesty beats a decimal place of invention.
        """
        if not self.total:
            return -1
        target = (self.total * fraction) // 100
        seen = 0
        for index in range(len(self.counts)):
            seen += self.counts[index]
            if seen > target:
                return self._upper(index)
        return self._upper(len(self.counts) - 1)

    def fields(self):
        return {
            "n": self.total,
            "min": self.min if self.min is not None else -1,
            "max": self.max if self.max is not None else -1,
            "p50": self.percentile(50),
            "p90": self.percentile(90),
            "p99": self.percentile(99),
            "fine_us": FINE_US,
            "fine_buckets": FINE_BUCKETS,
            "coarse_us": ",".join(str(e) for e in COARSE_EDGES_US),
            "counts": ",".join(str(c) for c in self.counts),
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
    collects = 0
    free_floor = gc.mem_free()

    # Everything this loop touches is allocated before the load is pinned, so
    # the measurement adds nothing to the heap it is measuring.
    histogram = Histogram()

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
        histogram.add((monotonic_ns() - started) // 1000)

    _emit(
        "RESULT",
        case="loop",
        available=1,
        machine=1 if machine is not None else 0,
        passes=passes,
        collects=collects,
        free_floor=free_floor,
        **histogram.fields()
    )


def case_body(monotonic_ns, machine, passes=LOOP_PASSES // 2):
    """What the typical pass is made of.

    The tail is settled - every pass over 20 ms contains a collection - but the
    8.5 ms body is not, and the rewrite needs to know whether that is the engine
    or the screen. Timing the two halves separately is the first cut; if
    `machine.update()` dominates, the next question is which part of it, and
    that one needs SamplerState opened up.

    Passes that collect are counted but excluded from both histograms. A
    collection lands inside whichever half was running when the floor was
    crossed and would otherwise smear 25 ms across one of them at random.
    """
    _start("body")
    if monotonic_ns is None or machine is None:
        _emit("RESULT", case="body", available=0)
        return

    import guard

    gc_floor = 16 * 1024
    engine_us = Histogram()
    machine_us = Histogram()
    skipped = 0

    _pin_load()
    gc.collect()

    for _ in range(passes):
        guard.feed()
        if gc.mem_free() < gc_floor:
            gc.collect()
            skipped += 1
            continue
        started = monotonic_ns()
        engine.tick()
        middle = monotonic_ns()
        machine.update()
        ended = monotonic_ns()
        engine_us.add((middle - started) // 1000)
        machine_us.add((ended - middle) // 1000)

    engine_fields = engine_us.fields()
    machine_fields = machine_us.fields()
    _emit(
        "RESULT",
        case="body_engine",
        available=1,
        skipped=skipped,
        **engine_fields
    )
    _emit(
        "RESULT",
        case="body_machine",
        available=1,
        skipped=skipped,
        **machine_fields
    )


def case_gc(monotonic_ns):
    """How long a collection takes, and how often one is needed under load."""
    _start("gc")
    if monotonic_ns is None:
        _emit("RESULT", case="gc", available=0)
        return

    histogram = Histogram()
    _pin_load()
    for _ in range(24):
        # Let the loop generate real garbage between collections rather than
        # timing a collect on an already-clean heap, which is the fast case and
        # not the one that interrupts audio.
        for _ in range(200):
            engine.tick()
        started = monotonic_ns()
        gc.collect()
        histogram.add((monotonic_ns() - started) // 1000)

    _emit(
        "RESULT",
        case="gc",
        available=1,
        free_after=gc.mem_free(),
        **histogram.fields()
    )


def _real_machine():
    """The state machine main.py runs, brought up to the sampler screen.

    Measuring the loop without this measures `engine.tick()` and calls it the
    loop. The shipping body is `guard.feed(); gc check; engine.tick();
    machine.update()`, and `machine.update()` is what draws the display and the
    LEDs - on a badge where a full frame over I2C is tens of milliseconds, that
    is not a rounding error.

    Startup is stepped rather than jumped: `go_to_state("sampler")` would skip
    the warming that StartupState does, and the sampler screen it builds is not
    the same object as the one a warmed badge is holding.
    """
    import statemachine

    machine = statemachine.StateMachine()
    machine.go_to_state("startup")
    for _ in range(6000):
        engine.tick()
        machine.update()
        state = machine.state
        if state is not None and state.name == "sampler":
            return machine
    # Startup did not finish. Say so rather than silently measuring a banner.
    _emit("CASE", case="machine", state="STARTUP_INCOMPLETE")
    return machine


def run(machine=None, with_machine=True):
    """Every case, in order, each announced before it runs.

    `with_machine` builds the real state machine so the loop figure is the
    shipping loop. Passing False measures the engine alone, which is the lower
    bound and is only useful for the difference between the two.
    """
    _emit("SPIKE", name="baseline", version=REPORT_VERSION)
    monotonic_ns = case_timebase()
    case_constants()
    if machine is None and with_machine:
        machine = _real_machine()
    _emit("CASE", case="machine", state="READY" if machine else "ABSENT")
    case_loop(monotonic_ns, machine)
    case_body(monotonic_ns, machine)
    case_gc(monotonic_ns)
    _emit("DONE", spike="baseline")
