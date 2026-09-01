cmake_minimum_required(VERSION 3.13)

# The toolchain and SDK live under ~/toolchains and were installed without root:
# the ARM GNU tarball unpacked in place, and the SDK as a shallow clone. Nothing
# here is on the system path, so both are pointed at explicitly rather than
# assumed.
if(NOT DEFINED ENV{PICO_SDK_PATH})
    set(ENV{PICO_SDK_PATH} "$ENV{HOME}/toolchains/pico-sdk")
endif()
if(NOT DEFINED ENV{PICO_TOOLCHAIN_PATH})
    file(GLOB _arm_toolchain "$ENV{HOME}/toolchains/arm-gnu-toolchain-*")
    if(_arm_toolchain)
        list(GET _arm_toolchain 0 _arm_toolchain_first)
        set(ENV{PICO_TOOLCHAIN_PATH} "${_arm_toolchain_first}")
    endif()
endif()

set(PICO_BOARD pico_w)
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(dczia_spikes C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

# --- variant (a): the common base ------------------------------------------
#
# Every later spike compiles against this and nothing else. It is deliberately
# not a nine-peripheral bring-up: the number Phase 0 wants from here is the
# FIXED overhead of the runtime, and a scaffold that initialises everything
# would both prejudge the C-versus-Rust question and make "free SRAM" mean
# whatever that particular set of drivers happened to allocate.
add_executable(spike_scaffold
    spike_scaffold.c
    spike_common.c
)
target_include_directories(spike_scaffold PRIVATE ${CMAKE_CURRENT_LIST_DIR})
target_link_libraries(spike_scaffold
    pico_stdlib
    pico_multicore
    hardware_watchdog
    hardware_clocks
)
# CDC only. No UART: the badge's UART pins are the MIDI jack, and stdio on them
# would put debug text down a MIDI cable.
pico_enable_stdio_usb(spike_scaffold 1)
pico_enable_stdio_uart(spike_scaffold 0)
pico_add_extra_outputs(spike_scaffold)

# A linker map for every variant. The whole point of this task is reading
# .data/.bss/stack sizes out of it rather than guessing at them.
target_link_options(spike_scaffold PRIVATE
    "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/spike_scaffold.map"
)

# --- Task 4: mixer cost and bank contention --------------------------------
#
# hardware_dma is here for the contention case, not for audio: a DMA hammering
# the striped SRAM is what makes the mixer's cycle count mean something on a bus
# it has to share.
add_executable(spike_mixer
    spike_mixer.c
    spike_common.c
)
target_include_directories(spike_mixer PRIVATE ${CMAKE_CURRENT_LIST_DIR})
target_link_libraries(spike_mixer
    pico_stdlib
    hardware_watchdog
    hardware_clocks
    hardware_dma
)
pico_enable_stdio_usb(spike_mixer 1)
pico_enable_stdio_uart(spike_mixer 0)
pico_add_extra_outputs(spike_mixer)
target_link_options(spike_mixer PRIVATE
    "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/spike_mixer.map"
)

# --- Task 5: output-path latency and the flash/XIP hazard -------------------
add_executable(spike_output
    spike_output.c
    spike_common.c
)
target_include_directories(spike_output PRIVATE ${CMAKE_CURRENT_LIST_DIR})
pico_generate_pio_header(spike_output ${CMAKE_CURRENT_LIST_DIR}/i2s.pio)
target_link_libraries(spike_output
    pico_stdlib
    hardware_watchdog
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_flash
)
pico_enable_stdio_usb(spike_output 1)
pico_enable_stdio_uart(spike_output 0)
pico_add_extra_outputs(spike_output)
target_link_options(spike_output PRIVATE
    "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/spike_output.map"
)

# --- Task 6: storage timing -------------------------------------------------
#
# A raw block driver, deliberately without a filesystem. FatFS reentrancy is
# what broke streaming in the Python, so putting FatFS back in would measure the
# layer that failed rather than the card underneath it.
add_executable(spike_sd
    spike_sd.c
    spike_common.c
)
target_include_directories(spike_sd PRIVATE ${CMAKE_CURRENT_LIST_DIR})
target_link_libraries(spike_sd
    pico_stdlib
    hardware_watchdog
    hardware_clocks
    hardware_dma
    hardware_spi
)
pico_enable_stdio_usb(spike_sd 1)
pico_enable_stdio_uart(spike_sd 0)
pico_add_extra_outputs(spike_sd)
target_link_options(spike_sd PRIVATE
    "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/spike_sd.map"
)

# --- Task 2 variants (b) and (c): what a USB class costs in SRAM ------------
#
# Link-only. The figure wanted is the FIXED overhead a class adds - endpoint
# buffers and per-instance structs declared at file scope - and that is settled
# at compile time. These builds are never flashed, and they do not enumerate the
# classes they link: descriptors would change flash, not SRAM.
function(spike_usb_variant name)
    add_executable(${name} spike_scaffold.c spike_common.c usb_class_stubs.c)
    target_include_directories(${name} PRIVATE ${CMAKE_CURRENT_LIST_DIR})
    target_link_libraries(${name} pico_stdlib pico_multicore hardware_watchdog
                          hardware_clocks)
    pico_enable_stdio_usb(${name} 1)
    pico_enable_stdio_uart(${name} 0)
    target_compile_definitions(${name} PRIVATE ${ARGN})
    target_link_options(${name} PRIVATE
        "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/${name}.map")
endfunction()

# The buffer sizes ARE the overhead, so they are chosen here explicitly rather
# than inherited from a default that might move between SDK versions:
#   MIDI 64-byte RX/TX/EP - one full-speed bulk packet, TinyUSB's own default
#     for a full-speed device, and what pico_stdio_usb already uses for CDC.
#   MSC  512-byte EP buffer - one SD block, the smallest size that does not
#     split every transfer.
spike_usb_variant(spike_usb_a)
spike_usb_variant(spike_usb_b
    CFG_TUD_MIDI=1
    CFG_TUD_MIDI_RX_BUFSIZE=64
    CFG_TUD_MIDI_TX_BUFSIZE=64
    CFG_TUD_MIDI_EP_BUFSIZE=64)
spike_usb_variant(spike_usb_c
    CFG_TUD_MIDI=1
    CFG_TUD_MIDI_RX_BUFSIZE=64
    CFG_TUD_MIDI_TX_BUFSIZE=64
    CFG_TUD_MIDI_EP_BUFSIZE=64
    CFG_TUD_MSC=1
    CFG_TUD_MSC_EP_BUFSIZE=512)
