# Baut libtracksy_whisper.so: whisper.cpp (third_party/, statisch) + tracksy_whisper.c.
# Aufruf über scripts/build-whisper.sh. whisper.cpp wird als Unterprojekt eingebunden
# (nicht „standalone“), damit dessen Build nichts in den Quellbaum schreibt.
cmake_minimum_required(VERSION 3.14)
project(tracksy_whisper C CXX)

set(WHISPER_SRC "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/whisper.cpp" CACHE PATH "whisper.cpp-Quellbaum")

# Nur CPU, kein Netzwerk, keine Downloads während des Builds
set(FETCHCONTENT_FULLY_DISCONNECTED ON CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
foreach(opt WHISPER_BUILD_EXAMPLES WHISPER_BUILD_TESTS WHISPER_BUILD_SERVER WHISPER_CURL WHISPER_SDL2
            WHISPER_COREML WHISPER_OPENVINO WHISPER_VITISAI
            GGML_RPC GGML_BLAS GGML_CUDA GGML_HIP GGML_MUSA GGML_SYCL GGML_VULKAN GGML_METAL
            GGML_OPENCL GGML_CANN GGML_WEBGPU GGML_ZDNN GGML_OPENMP_FETCH GGML_BACKEND_DL
            GGML_CPU_KLEIDIAI)
    set(${opt} OFF CACHE BOOL "" FORCE)
endforeach()
set(GGML_OPENMP ON CACHE BOOL "" FORCE)

add_subdirectory("${WHISPER_SRC}" whisper EXCLUDE_FROM_ALL)

add_library(tracksy_whisper SHARED tracksy_whisper.c)
target_link_libraries(tracksy_whisper PRIVATE whisper)
target_link_options(tracksy_whisper PRIVATE -Wl,--exclude-libs,ALL -Wl,--no-undefined)
set_target_properties(tracksy_whisper PROPERTIES C_VISIBILITY_PRESET hidden)
