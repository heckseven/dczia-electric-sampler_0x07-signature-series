/* Raw SD block reads over SPI. No filesystem, no writes.
 *
 * Phase 0 measured this card at 476 us per block and 1,524 KB/s sequential at
 * 20.83 MHz, across 615,000 reads without a single failure - so the sequence in
 * sd.c is the spike's, kept rather than written again from the spec.
 *
 * Two things it deliberately does not do:
 *
 *   No FatFS. Reentrancy in FatFS is what broke streaming in the CircuitPython
 *   firmware, and Phase 1 only needs to read files at load time.
 *
 *   No writes. Saving belongs to a later phase, and a driver that cannot write
 *   cannot corrupt the player's card while the rest of this is still moving.
 */

#ifndef SD_H
#define SD_H

#include <stdbool.h>
#include <stdint.h>

#define SD_BLOCK 512

/* Asking for 24 MHz gets 20.83: clk_peri is 125 MHz and the SPI prescaler only
 * divides in steps. Phase 0 swept 20.83 / 15.63 / 7.81 and found the fastest
 * both quickest and no less reliable. */
#define SD_BAUDRATE 24000000

bool sd_init(void);
bool sd_read(uint32_t lba, uint8_t *buffer);

/* Write one block. The card holds the bus while it programs - milliseconds -
 * and this waits for it rather than returning early. */
bool sd_write(uint32_t lba, const uint8_t *buffer);

/* Write, then read back and compare.
 *
 * Used for everything that is filesystem structure rather than file contents.
 * A wrong byte in a FAT or a directory entry is not a wrong song, it is a card
 * that will not mount, and finding out later is not acceptable. A verifying
 * read costs 476 us against the milliseconds the write already took. */
bool sd_write_verified(uint32_t lba, const uint8_t *buffer);

/* Capacity in 512-byte blocks, from the CSD, across the full 22 bits of C_SIZE.
 *
 * Phase 0 found CircuitPython's sdcardio reporting exactly the value a 16-bit
 * truncation gives - 58,064,896 blocks where the card holds 125,173,760. That
 * is harmless on a card partitioned below 29.7 GB and silently addresses the
 * wrong blocks above it. */
/* Something to run while the card is busy programming a block.
 *
 * A save holds the bus for tens of milliseconds. The audio does not care - it
 * is on the other core - but anything on this one with a deadline does, and the
 * sequencer is the obvious example. Set it to whatever needs to keep running,
 * or leave it unset. */
void sd_set_idle_hook(void (*hook)(void));

uint32_t sd_blocks(void);

#endif /* SD_H */
