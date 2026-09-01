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
