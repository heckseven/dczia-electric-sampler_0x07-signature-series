/* Phase 1 criterion 6: does the display perturb the audio the instrument emits?
 *
 * Task 7 answered this for a rig where the audio refill ran in an interrupt on
 * core 0. Phase 1 does not look like that: audio owns core 1 and polls, the
 * display runs from core 0, and the two use different DMA channels. That is a
 * different arrangement - arguably a safer one - and Task 7's own caveat says
 * so: "A design that later shares a DMA channel or an IRQ priority between the
 * panel and the audio path would need it re-run."
 *
 * They still share a bus and they still share SRAM. So it is re-run, against
 * the shipped mixer and the shipped display driver.
 */

#include <stdio.h>

#include "hardware/clocks.h"
#include "pico/stdlib.h"

#include "audio.h"
#include "console.h"
#include "display.h"

#define CAPTURE_BLOCKS 400
#define TEST_VALUE 0x0100

static uint32_t run(bool hammer) {
    audio_capture_arm(CAPTURE_BLOCKS);
    while (!audio_capture_done()) {
        if (hammer) {
            /* Change every page every frame, so the flush cannot skip any of
             * them - the shipped driver only writes what actually differs, and
             * a null test that let it idle would prove nothing. */
            static bool phase;
            phase = !phase;
            for (uint32_t y = 0; y < OLED_HEIGHT; y += 2) {
                display_fill_rect(0, y, OLED_WIDTH, 1, phase);
            }
            display_flush();
        }
        console_pump();
    }
    return audio_capture_sum();
}

int main(void) {
    console_begin("rt-null");
    audio_init();
    display_init();

    uint32_t frames = SAMPLE_RATE; /* one second, longer than the capture */
    int16_t *tone = audio_arena_alloc(frames);
    for (uint32_t i = 0; i < frames; i++) {
        /* Something with structure, so a checksum over it is sensitive to a
         * single sample moving rather than to gross silence. */
        tone[i] = (int16_t)(((i * 37) % (TEST_VALUE * 2)) - TEST_VALUE);
    }
    audio_set_sample(0, tone, frames);
    audio_set_pitch(0, PITCH_UNITY);
    audio_set_gain(0, 0x7FFF);
    audio_set_master(0x4000);
    audio_start();

    busy_wait_us(100000);

    uint32_t quiet = run(false);
    uint32_t quiet_under = audio_underruns();
    uint32_t busy = run(true);
    uint32_t busy_under = audio_underruns();

    printf("RESULT case=null blocks=%d quiet_sum=0x%08lx busy_sum=0x%08lx "
           "identical=%d underruns_quiet=%lu underruns_busy=%lu\n",
           CAPTURE_BLOCKS, (unsigned long)quiet, (unsigned long)busy,
           quiet == busy ? 1 : 0, (unsigned long)quiet_under,
           (unsigned long)busy_under);
    printf("DONE spike=rt-null\n");

    audio_stop_all();
    while (true) {
        console_pump();
        sleep_ms(10);
    }
}
