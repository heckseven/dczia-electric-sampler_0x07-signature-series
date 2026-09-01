/* Enough of the MSC callbacks to link, and nothing more.
 *
 * Variants (b) and (c) exist to answer one question: how much SRAM does adding
 * a USB class cost? That cost is static - endpoint buffers and per-instance
 * structs the class driver declares at file scope - so it is fully determined
 * once the class is compiled in, and reading it out of the linker map needs a
 * build that links, not a device that enumerates.
 *
 * These stubs are therefore deliberately non-functional. A real MSC device
 * would serve blocks from the SD card; measuring that is Task 6's job, and
 * wiring it up here would put an SD driver's buffers into a number that is
 * supposed to be the USB class's.
 */

#include <stdint.h>
#include <string.h>

#include "tusb.h"

#if CFG_TUD_MSC

void tud_msc_inquiry_cb(uint8_t lun, uint8_t vendor_id[8],
                        uint8_t product_id[16], uint8_t product_rev[4]) {
    (void)lun;
    memcpy(vendor_id, "DCZia   ", 8);
    memcpy(product_id, "Spike measure   ", 16);
    memcpy(product_rev, "1.0 ", 4);
}

bool tud_msc_test_unit_ready_cb(uint8_t lun) {
    (void)lun;
    return false; /* never ready: nothing here should be mounted */
}

void tud_msc_capacity_cb(uint8_t lun, uint32_t *block_count,
                         uint16_t *block_size) {
    (void)lun;
    *block_count = 0;
    *block_size = 512;
}

bool tud_msc_start_stop_cb(uint8_t lun, uint8_t power_condition, bool start,
                           bool load_eject) {
    (void)lun;
    (void)power_condition;
    (void)start;
    (void)load_eject;
    return true;
}

int32_t tud_msc_read10_cb(uint8_t lun, uint32_t lba, uint32_t offset,
                          void *buffer, uint32_t bufsize) {
    (void)lun;
    (void)lba;
    (void)offset;
    (void)buffer;
    (void)bufsize;
    return -1;
}

int32_t tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset,
                           uint8_t *buffer, uint32_t bufsize) {
    (void)lun;
    (void)lba;
    (void)offset;
    (void)buffer;
    (void)bufsize;
    return -1;
}

int32_t tud_msc_scsi_cb(uint8_t lun, uint8_t const scsi_cmd[16], void *buffer,
                        uint16_t bufsize) {
    (void)lun;
    (void)scsi_cmd;
    (void)buffer;
    (void)bufsize;
    return -1;
}

#endif /* CFG_TUD_MSC */
