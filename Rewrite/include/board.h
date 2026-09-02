/* Every pin the badge uses, in one place.
 *
 * Transcribed from Software/Production/setup.py, which is the only authority on
 * this board - there is no schematic in the repository, and the CircuitPython
 * firmware is what has been proven to work against real hardware. Where the
 * Python names a pin, the line is quoted, so a future reader can check the
 * transcription without opening the other file.
 *
 * Guessing a pin here does not produce a compile error, it produces a badge
 * that does something strange. That is why this file exists rather than each
 * driver naming its own.
 */

#ifndef BOARD_H
#define BOARD_H

/* audiobusio.I2SOut(board.GP0, board.GP1, board.GP2)
 *
 * CircuitPython's argument order is (bit_clock, word_select, data). The PIO
 * program side-sets two consecutive pins, so BCLK and LRCLK must stay adjacent
 * and in that order. */
#define PIN_I2S_BCLK 0
#define PIN_I2S_LRCLK 1 /* must be PIN_I2S_BCLK + 1 */
#define PIN_I2S_DATA 2

/* neopixel.NeoPixel(board.GP3, 10, brightness=0.1) */
#define PIN_NEOPIXEL 3
#define NEOPIXEL_COUNT 10

/* rotaryio.IncrementalEncoder(board.GP4, board.GP5) */
#define PIN_ENC_VOLUME_A 4
#define PIN_ENC_VOLUME_B 5

/* digitalio on board.GP6 (in) and board.GP7 (out) */
#define PIN_SYNC_IN 6
#define PIN_SYNC_OUT 7

/* busio.UART(tx=board.GP8, rx=board.GP9, baudrate=31250)
 *
 * These are the MIDI jack. Nothing may put stdio on them: debug text down a
 * MIDI cable is indistinguishable from a stuck note. */
#define PIN_MIDI_TX 8
#define PIN_MIDI_RX 9

/* busio.SPI(board.GP10, board.GP11, board.GP12) is (clock, MOSI, MISO),
 * then sdcardio.SDCard(spi, board.GP13). GP10-13 are SPI1 on RP2040. */
#define PIN_SD_SCK 10
#define PIN_SD_MOSI 11
#define PIN_SD_MISO 12
#define PIN_SD_CS 13
#define SD_SPI spi1

/* busio.I2C(board.GP15, board.GP14) is (scl, sda). GP14/15 are I2C1. */
#define PIN_OLED_SDA 14
#define PIN_OLED_SCL 15
#define OLED_I2C i2c1
#define OLED_ADDR 0x3C
#define OLED_WIDTH 128
#define OLED_HEIGHT 32

/* rotaryio.IncrementalEncoder(board.GP16, board.GP17) */
#define PIN_ENC_SELECT_A 16
#define PIN_ENC_SELECT_B 17

/* keypad.KeyMatrix(row_pins=(GP27, GP26, GP18),
 *                  column_pins=(GP20, GP21, GP22, GP28))
 *
 * Three rows by four columns is twelve keys: eight pads and four modifiers.
 * The rows are deliberately not consecutive - that is the board, not a typo. */
#define KEY_ROWS 3
#define KEY_COLS 4
#define KEY_COUNT (KEY_ROWS * KEY_COLS)
static const unsigned char PIN_KEY_ROWS[KEY_ROWS] = {27, 26, 18};
static const unsigned char PIN_KEY_COLS[KEY_COLS] = {20, 21, 22, 28};

/* --- audio format, fixed by the samples on the card ----------------------- */

#define SAMPLE_RATE 16000
#define CHANNELS 1
#define BITS_PER_SAMPLE 16

/* Frames per DMA block, and therefore the latency.
 *
 * Phase 0 measured trigger-to-output as block_period x (1..2) + 568 us. At 32
 * frames that is 2.00 ms per block and a 4.56 ms worst case - half the 10 ms
 * budget, leaving room for the mixer and for the PIO TX FIFO to stay joined at
 * 8 words. 16 frames would give 2.56 ms if something ever needs it, at twice
 * the interrupt rate. */
#define BLOCK_FRAMES 32

/* Two PIO cycles per bit, 32 bit clocks per stereo frame. */
#define I2S_BITS_PER_FRAME 64

#endif /* BOARD_H */
