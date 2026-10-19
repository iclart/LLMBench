find_package(Threads REQUIRED)

add_library(railcommon STATIC
  tools/common/answers.cpp
  tools/common/benchlog.cpp
  tools/common/config.cpp
  tools/common/rules.cpp
  tools/common/suite.cpp
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

# Linux-only: the trusted runner used inside the evaluation sandbox.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  add_executable(railexec tools/exec/main.cpp)
endif()

add_executable(railjudge tools/judge/main.cpp)
target_link_libraries(railjudge PRIVATE railcommon)
if(MINGW)
  target_link_options(railjudge PRIVATE -static)
endif()

# railrun_<name>: single-threaded driver; railbench_<name>: concurrent harness.
function(add_rail_engine name)
  add_executable(railrun_${name} tools/run/main.cpp ${ARGN})
  add_executable(railbench_${name} tools/bench/main.cpp ${ARGN})
  foreach(t railrun_${name} railbench_${name})
    target_link_libraries(${t} PRIVATE railcommon Threads::Threads)
    if(MINGW)
      target_link_options(${t} PRIVATE -static)
    endif()
  endforeach()
endfunction()

add_rail_engine(baseline reference/baseline/baseline.cpp)
add_rail_engine(oracle reference/oracle/oracle.cpp)
add_rail_engine(stub stub/solution.cpp)

# The strong reference lives in a private repository, checked out as the
# `private` submodule at the repository root. Everything else builds without it.
set(RAIL_STRONG_DIR ${PROJECT_SOURCE_DIR}/private/railway/strong CACHE PATH "Private v1 reference directory")
if(EXISTS ${RAIL_STRONG_DIR}/strong.cpp)
  file(GLOB RAIL_STRONG_SOURCES CONFIGURE_DEPENDS ${RAIL_STRONG_DIR}/*.cpp)
  add_rail_engine(strong ${RAIL_STRONG_SOURCES})
  set(RAIL_HAVE_STRONG ON)
else()
  message(STATUS "railway: private/ not checked out, skipping the strong reference")
  set(RAIL_HAVE_STRONG OFF)
endif()

# Candidates never replace a season's strong anchor without a measured gate.
set(RAIL_STRONG_CANDIDATE "" CACHE FILEPATH "Private experimental v1 engine")
if(RAIL_STRONG_CANDIDATE)
  add_rail_engine(candidate ${RAIL_STRONG_CANDIDATE})
  option(RAIL_PROFILE_CANDIDATE "Build a separately instrumented private candidate" OFF)
  if(RAIL_PROFILE_CANDIDATE)
    set(RAIL_PROFILE_SOURCE ${RAIL_STRONG_CANDIDATE} CACHE FILEPATH "Separately instrumented private candidate copy")
    add_rail_engine(profile ${RAIL_PROFILE_SOURCE})
    target_compile_definitions(railrun_profile PRIVATE RAIL_PROFILE=1)
    target_compile_definitions(railbench_profile PRIVATE RAIL_PROFILE=1)
  endif()
endif()

set(RAIL_PROFILE_FROZEN "" CACHE FILEPATH "Separately instrumented private frozen source")
if(RAIL_PROFILE_FROZEN)
  add_rail_engine(profile_frozen ${RAIL_PROFILE_FROZEN})
  target_compile_definitions(railrun_profile_frozen PRIVATE RAIL_PROFILE=1)
  target_compile_definitions(railbench_profile_frozen PRIVATE RAIL_PROFILE=1)
endif()

add_subdirectory(tests)
add_subdirectory(v2)
