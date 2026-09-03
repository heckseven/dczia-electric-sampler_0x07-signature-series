/* Host tests for the msgpack reader and writer.
 *
 * Encoding is arithmetic, and arithmetic is better enumerated on a laptop than
 * sampled on a badge. The float encoder in particular produced exactly twice
 * the value it was given, which reads back as a plausible track volume rather
 * than as an error - the kind of bug that survives a hardware test because
 * nothing about it looks wrong.
 */

#include <stdio.h>
#include <string.h>

#include "msgpack.h"

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

static void test_float_round_trip(void) {
    /* Every value the track-volume range can take, at the resolution Q12 gives
     * it, rather than a handful of convenient ones. */
    for (int32_t milli = 0; milli <= 2000; milli += 1) {
        uint8_t buffer[16];
        struct mpw w;
        mpw_init(&w, buffer, sizeof(buffer));
        mpw_float_milli(&w, milli);
        if (!w.ok) {
            check(false, "float encodes");
            return;
        }
        struct mp r;
        mp_init(&r, buffer, w.at);
        int32_t back = -1;
        if (!mp_float(&r, &back)) {
            check(false, "float decodes");
            return;
        }
        /* Thousandths through a 24-bit mantissa: exact for these values, and a
         * tolerance of one absorbs the division rather than hiding a factor. */
        int32_t error = back - milli;
        if (error < -1 || error > 1) {
            printf("FAIL float %d came back %d\n", milli, back);
            failures++;
            return;
        }
    }
}

static void test_known_encodings(void) {
    /* Against the IEEE-754 bit patterns, so a self-consistent-but-wrong pair of
     * encoder and decoder cannot pass. */
    struct {
        int32_t milli;
        uint8_t bytes[4];
    } cases[] = {
        {1000, {0x3F, 0x80, 0x00, 0x00}}, /* 1.0 */
        {1500, {0x3F, 0xC0, 0x00, 0x00}}, /* 1.5 */
        {500, {0x3F, 0x00, 0x00, 0x00}},  /* 0.5 */
        {2000, {0x40, 0x00, 0x00, 0x00}}, /* 2.0 */
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        uint8_t buffer[16];
        struct mpw w;
        mpw_init(&w, buffer, sizeof(buffer));
        mpw_float_milli(&w, cases[i].milli);
        check(w.at == 5 && buffer[0] == 0xCA, "float is a five-byte float32");
        check(memcmp(&buffer[1], cases[i].bytes, 4) == 0,
              "float matches the IEEE-754 pattern");
    }
}

static void test_ints_and_nil(void) {
    uint8_t buffer[64];
    struct mpw w;
    mpw_init(&w, buffer, sizeof(buffer));
    mpw_int(&w, 0);
    mpw_int(&w, 127);
    mpw_int(&w, 128);
    mpw_int(&w, 300);
    mpw_int(&w, -1);
    mpw_int(&w, -32);
    mpw_nil(&w);
    mpw_bool(&w, true);
    check(w.ok, "ints encode");

    struct mp r;
    mp_init(&r, buffer, w.at);
    int64_t v;
    static const int64_t expected[] = {0, 127, 128, 300, -1, -32};
    for (unsigned i = 0; i < sizeof(expected) / sizeof(expected[0]); i++) {
        check(mp_int(&r, &v) && v == expected[i], "int round trip");
    }
    check(mp_nil(&r), "nil is recognised");
    check(mp_int(&r, &v) && v == 1, "true reads as one");
}

static void test_skip_covers_what_the_python_writes(void) {
    /* A map holding the shapes store.Store produces, skipped wholesale. */
    uint8_t buffer[128];
    struct mpw w;
    mpw_init(&w, buffer, sizeof(buffer));
    mpw_array(&w, 3);
    mpw_nil(&w);
    mpw_float_milli(&w, 1000);
    mpw_str(&w, "kit_name");

    struct mp r;
    mp_init(&r, buffer, w.at);
    check(mp_skip(&r), "an array of nil, float and string skips");
    check(r.at == w.at, "skip consumes exactly the array");
}

int main(void) {
    test_float_round_trip();
    test_known_encodings();
    test_ints_and_nil();
    test_skip_covers_what_the_python_writes();

    if (failures == 0) {
        printf("ok - all msgpack tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
