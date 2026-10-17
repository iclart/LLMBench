add_executable(railtests
  railtests.cpp
  ../tools/gen/gen_world.cpp
  ../tools/gen/gen_load.cpp
  ../stub/solution.cpp
)
target_include_directories(railtests PRIVATE ../tools/gen)
target_link_libraries(railtests PRIVATE railcommon)
target_compile_definitions(railtests PRIVATE RAIL_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
if(MINGW)
  target_link_options(railtests PRIVATE -static)
endif()

add_test(NAME railtests COMMAND railtests)
