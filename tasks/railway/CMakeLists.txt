add_library(railcommon STATIC
  tools/common/config.cpp
  tools/common/rules.cpp
  tools/common/workload.cpp
  tools/common/world.cpp
)
target_include_directories(railcommon PUBLIC include tools/common)

add_executable(railgen
  tools/gen/main.cpp
  tools/gen/gen_world.cpp
  tools/gen/gen_load.cpp
)
target_include_directories(railgen PRIVATE tools/gen)
target_link_libraries(railgen PRIVATE railcommon)

# Ship self-contained binaries on Windows (no MinGW runtime DLLs needed).
if(MINGW)
  target_link_options(railgen PRIVATE -static)
endif()

add_subdirectory(tests)
