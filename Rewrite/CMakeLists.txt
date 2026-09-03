cmake_minimum_required(VERSION 3.13)

# Same toolchain discovery as the Spikes build: nothing is on the system path,
# so both the SDK and the ARM toolchain are pointed at explicitly.
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

project(dczia_rt C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

add_executable(rt
    src/main.c
    src/audio.c
    src/console.c
    src/sd.c
    src/fat.c
    src/kit.c
    src/input.c
    src/display.c
)
target_include_directories(rt PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
target_link_libraries(rt
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_i2c
    hardware_spi
    hardware_watchdog
)
# CDC only. The badge's UART pins are the MIDI jack, and stdio on them would put
# debug text down a MIDI cable.
pico_enable_stdio_usb(rt 1)
pico_enable_stdio_uart(rt 0)
pico_add_extra_outputs(rt)
target_link_options(rt PRIVATE "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/rt.map")

# --- criterion 2: trigger-to-output, measured at the pin --------------------
#
# Links the shipped audio.c rather than a copy, so the number describes the
# instrument. Never flashed as the instrument: it adds a second PIO program
# whose only job is to watch the data pin.
add_executable(rt_latency
    measure/measure_latency.c
    src/audio.c
    src/console.c
)
target_include_directories(rt_latency PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt_latency ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
pico_generate_pio_header(rt_latency ${CMAKE_CURRENT_LIST_DIR}/measure/watch.pio)
target_link_libraries(rt_latency
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_watchdog
)
pico_enable_stdio_usb(rt_latency 1)
pico_enable_stdio_uart(rt_latency 0)
pico_add_extra_outputs(rt_latency)
