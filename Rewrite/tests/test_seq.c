/* Host tests for the pattern and the transport.
 *
 * These run on the build machine, not the badge. The parts worth testing here -
 * which step owns which tick, how offsets place a hit, whether tracks of
 * different lengths stay independent - are pure arithmetic, and arithmetic is
 * far better checked by enumerating every case than by listening to it.
 *
 * The badge measures what only the badge can: latency, jitter, underruns. This
 * measures what a laptop can do exhaustively in a millisecond.
 */

#include <stdio.h>
#include <string.h>

#include "seq.h"
#include "song.h"

/* --- the audio the transport thinks it is talking to ---------------------- */

struct fired {
    uint8_t track;
    uint64_t frame;
    int16_t velocity;
};

static struct fired log[4096];
static uint32_t log_count;
static uint64_t fake_frames;

uint64_t audio_frames(void) {
    return fake_frames;
}

void audio_trigger_at_frame(uint8_t track, uint64_t at_frame,
                            int16_t velocity) {
    if (log_count < 4096) {
        log[log_count].track = track;
        log[log_count].frame = at_frame;
        log[log_count].velocity = velocity;
        log_count++;
    }
}

void audio_stop_all(void) {}

/* --- harness -------------------------------------------------------------- */

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

/* Run the transport across `steps` steps, feeding it frames the way the audio
 * core would.
 *
 * Stops just short of the closing boundary. A run of exactly N steps fires N+1
 * times - once at frame zero and once on the boundary that ends it - and
 * counting that as N is a fencepost, not a bug in the transport. Asking for
 * "not quite N steps" makes the expected count unambiguous. */
static void run_steps(struct seq *seq, uint32_t steps) {
    log_count = 0;
    fake_frames = 0;
    seq_start(seq);
    uint64_t per_step = (((uint64_t)SAMPLE_RATE * 60u << 32) /
                         ((uint64_t)seq->song->bpm * PPQN)) *
                        song_ticks_per_step(seq->song);
    uint64_t end = ((per_step * steps) >> 32) - BLOCK_FRAMES;
    while (fake_frames < end) {
        seq_update(seq);
        fake_frames += BLOCK_FRAMES;
    }
}

static uint32_t hits_on(uint8_t track) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < log_count; i++) {
        if (log[i].track == track) {
            n++;
        }
    }
    return n;
}

/* --- tests ---------------------------------------------------------------- */

static void test_every_step_fires_once(void) {
    struct song song;
    struct seq seq;
    song_init(&song);
    song_set_length(&song, 0, 8);
    for (uint32_t s = 0; s < 8; s++) {
        song_set_step(&song, 0, s, VELOCITY_DEFAULT, 0);
    }
    for (uint32_t t = 1; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 8);
    }
    seq_init(&seq, &song);

    run_steps(&seq, 16);
    check(hits_on(0) == 16, "eight steps fire twice over two cycles");
}

static void test_polyrhythm(void) {
    /* The point of per-track lengths.
     *
     * Both tracks step at the same rate - length does not change the rate, it
     * changes how long the pattern is before it repeats. So give each one a
     * single hit at its own step 0: the three-step track fires every third
     * step, the four-step track every fourth, and the two land together only
     * once every twelve. That is the polyrhythm, and filling both tracks with
     * hits on every step would hide it behind a coincidence on every beat. */
    struct song song;
    struct seq seq;
    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 4);
    }
    song_set_length(&song, 0, 3);
    song_set_length(&song, 1, 4);
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 0);
    song_set_step(&song, 1, 0, VELOCITY_DEFAULT, 0);
    seq_init(&seq, &song);

    run_steps(&seq, 12);
    check(hits_on(0) == 4, "a three-step track fires four times in twelve steps");
    check(hits_on(1) == 3, "a four-step track fires three times in twelve steps");

    uint32_t together = 0;
    for (uint32_t i = 0; i < log_count; i++) {
        for (uint32_t j = 0; j < log_count; j++) {
            if (log[i].track == 0 && log[j].track == 1 &&
                log[i].frame == log[j].frame) {
                together++;
            }
        }
    }
    check(together == 1, "the two cycles coincide once in twelve steps");
}

static void test_offsets_move_hits(void) {
    struct song song;
    struct seq seq;
    song_init(&song);
    song_set_length(&song, 0, 4);
    for (uint32_t t = 1; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 4);
    }
    /* 1/16 is six ticks, so the legal offset is +/- 2. */
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 0);
    song_set_step(&song, 0, 1, VELOCITY_DEFAULT, 2);   /* late */
    song_set_step(&song, 0, 2, VELOCITY_DEFAULT, -2);  /* early */
    song_set_step(&song, 0, 3, VELOCITY_DEFAULT, 0);
    seq_init(&seq, &song);
    /* Offsets only reach the scheduler at less than full quantise strength -
     * which is the default, since an offset is a record of how a hit was
     * played and the knob decides how much of that survives. This test is
     * about the transport honouring an offset, so it turns the knob off. */
    seq.strength = 0;

    check(song_max_offset(&song) == 2, "1/16 allows two ticks either way");

    run_steps(&seq, 4);
    check(hits_on(0) == 4, "every step still fires exactly once with offsets");

    /* The nudged pair should be closer together than a plain step apart: step 1
     * is two ticks late and step 2 is two ticks early, so four ticks separate
     * them where six would without offsets. */
    uint64_t frames[4];
    uint32_t n = 0;
    for (uint32_t i = 0; i < log_count && n < 4; i++) {
        if (log[i].track == 0) {
            frames[n++] = log[i].frame;
        }
    }
    if (n == 4) {
        uint64_t plain = frames[1] - frames[0];
        uint64_t nudged = frames[2] - frames[1];
        check(nudged < plain, "an early step lands sooner after a late one");
    } else {
        check(false, "four hits to compare");
    }
}

static void test_mute_and_velocity(void) {
    struct song song;
    struct seq seq;
    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 2);
    }
    song_set_step(&song, 0, 0, VELOCITY_MAX, 0);
    song_set_step(&song, 1, 0, VELOCITY_MAX, 0);
    song.muted[1] = 1;
    seq_init(&seq, &song);

    run_steps(&seq, 4);
    check(hits_on(0) > 0, "an unmuted track fires");
    check(hits_on(1) == 0, "a muted track does not");

    for (uint32_t i = 0; i < log_count; i++) {
        if (log[i].track == 0) {
            check(log[i].velocity > 0x7000, "full velocity is near full scale");
            break;
        }
    }
}

static void test_division_changes_clamp_offsets(void) {
    /* A shorter step means a smaller legal offset, and a hit left beyond the
     * new limit would sit closer to a neighbour's grid line than its own. */
    struct song song;
    song_init(&song);
    song_set_division(&song, DIVISION_QUARTER); /* 24 ticks, +/- 11 */
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 11);
    check(song_offset(&song, 0, 0) == 11, "a quarter allows eleven ticks");

    song_set_division(&song, DIVISION_THIRTYSECOND); /* 3 ticks, +/- 1 */
    check(song_max_offset(&song) == 1, "1/32 allows one tick");
    check(song_offset(&song, 0, 0) == 1, "the old offset is clamped, not kept");
}

static void test_recording_finds_the_nearest_step(void) {
    /* Recording asks where the transport is *audibly*, which is behind where it
     * has already booked. These are the cases that decide whether a hit lands
     * on the beat the player heard or the one after it.
     *
     * At 125 BPM a tick is exactly 320 frames (16000 * 60 / (125 * 24)), so the
     * arithmetic here is not itself rounding and the test measures seq_now
     * rather than its own constants. */
    struct song song;
    song_init(&song);
    song_set_bpm(&song, 125); /* 1/16 -> 6 ticks a step, 320 frames a tick */
    struct seq seq;
    seq_init(&seq, &song);

    fake_frames = 0;
    seq_start(&seq);

    const uint64_t frames_per_tick = 320;
    uint32_t step = 99;
    int32_t offset = 99;

    /* Dead on step 3. */
    fake_frames = seq.start_frame + 18 * frames_per_tick;
    check(seq_now(&seq, 0, &step, &offset), "a running transport has a now");
    check(step == 3, "on the boundary of step 3");
    check(offset == 0, "and no offset");

    /* Two ticks late. */
    fake_frames = seq.start_frame + 20 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 3, "two ticks late still belongs to step 3");
    check(offset == 2, "recorded as two ticks late");

    /* Two ticks early - the nearest step, not the one just passed. */
    fake_frames = seq.start_frame + 16 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 3, "two ticks early belongs to step 3, not step 2");
    check(offset == -2, "recorded as two ticks early");

    /* Past halfway it is the next step, early, rather than this one very
     * late - which is the whole reason for rounding rather than truncating. */
    fake_frames = seq.start_frame + 21 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 4, "past halfway it rounds up to step 4");
    check(offset == -3, "as three ticks early");

    /* Half a tick late still counts as on the beat: an offset is a whole
     * number of ticks, so the nearest one is the only answer available. */
    fake_frames = seq.start_frame + 18 * frames_per_tick + 159;
    seq_now(&seq, 0, &step, &offset);
    check(step == 3 && offset == 0, "just under half a tick rounds to the beat");

    /* And it wraps on the track's own length rather than running off. */
    fake_frames = seq.start_frame + 48 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 0, "step 8 of an eight-step track wraps to 0");

    seq_stop(&seq);
    check(!seq_now(&seq, 0, &step, &offset), "a stopped transport has no now");
}

static void test_quantize_strength_is_applied_on_playback(void) {
    /* The offset in the song is what the player did; strength decides how much
     * of it survives to the scheduler. Numbers checked against
     * engine/quantize.py's effective_offset, which this reproduces. */
    check(seq_effective_offset(5, STRENGTH_MAX) == 0, "full strength snaps");
    check(seq_effective_offset(5, 0) == 5, "zero strength plays as performed");
    check(seq_effective_offset(5, 10) == 3, "half of 5 rounds up to 3");
    check(seq_effective_offset(-5, 10) == -3, "and symmetrically the other way");
    check(seq_effective_offset(0, 0) == 0, "no offset stays no offset");
    check(seq_effective_offset(2, 15) == 1, "a quarter of 2 rounds to 1");
    check(seq_effective_offset(1, 19) == 0, "a twentieth of 1 rounds to none");

    /* And the transport actually uses it: the same song, unmodified, schedules
     * its hit at two different frames under two strengths. */
    struct song song;
    song_init(&song);
    song_set_length(&song, 0, 1);
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 2); /* two ticks late */
    for (uint32_t t = 1; t < TRACK_COUNT; t++) {
        song.lengths[t] = 0;
    }

    struct seq seq;
    seq_init(&seq, &song);
    check(seq.strength == STRENGTH_DEFAULT, "starts fully quantised");

    run_steps(&seq, 4);
    check(log_count > 0, "the pattern fires at full strength");
    uint64_t snapped = log[0].frame;

    seq_stop(&seq);
    seq.strength = 0;
    run_steps(&seq, 4);
    check(log_count > 0, "and at zero strength");
    uint64_t as_played = log[0].frame;

    check(as_played > snapped, "at zero strength the hit keeps its late offset");
    check(song_offset(&song, 0, 0) == 2, "and the song itself never changed");
}

int main(void) {
    test_every_step_fires_once();
    test_polyrhythm();
    test_offsets_move_hits();
    test_mute_and_velocity();
    test_division_changes_clamp_offsets();
    test_recording_finds_the_nearest_step();
    test_quantize_strength_is_applied_on_playback();

    if (failures == 0) {
        printf("ok - all sequencer tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
