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

# ---- engine checks on generated public data
set(CFG ${CMAKE_CURRENT_SOURCE_DIR}/../configs/public)
set(DATA ${CMAKE_CURRENT_BINARY_DIR}/data)
file(MAKE_DIRECTORY ${DATA})
set(SCENARIOS daily spring refund_wave)

add_test(NAME rail_gen_world COMMAND railgen world ${CFG}/world.cfg 1 ${DATA}/pub.world)
set_tests_properties(rail_gen_world PROPERTIES FIXTURES_SETUP rail_world)
foreach(s IN LISTS SCENARIOS)
  add_test(NAME rail_gen_${s} COMMAND railgen load ${DATA}/pub.world ${CFG}/${s}.cfg 2 ${DATA}/${s}.load)
  set_tests_properties(rail_gen_${s} PROPERTIES FIXTURES_REQUIRED rail_world FIXTURES_SETUP rail_data)
endforeach()

# The baseline is slow by design, so it only replays a prefix of each workload.
foreach(s IN LISTS SCENARIOS)
  add_test(NAME rail_verify_baseline_${s}
           COMMAND railrun_baseline verify ${DATA}/pub.world ${DATA}/${s}.load --limit 500)
  set_tests_properties(rail_verify_baseline_${s} PROPERTIES FIXTURES_REQUIRED rail_data)
endforeach()

if(RAIL_HAVE_STRONG)
  foreach(s IN LISTS SCENARIOS)
    add_test(NAME rail_verify_strong_${s} COMMAND railrun_strong verify ${DATA}/pub.world ${DATA}/${s}.load)
    set_tests_properties(rail_verify_strong_${s} PROPERTIES FIXTURES_REQUIRED rail_data)
  endforeach()

  # query() answers must match byte for byte between the two references.
  add_test(NAME rail_answers_baseline
           COMMAND railrun_baseline query ${DATA}/pub.world ${DATA}/daily.load ${DATA}/baseline.answers
                   --random 800 7)
  set_tests_properties(rail_answers_baseline PROPERTIES FIXTURES_REQUIRED rail_data FIXTURES_SETUP rail_answers)
  add_test(NAME rail_crosscheck_query
           COMMAND railrun_strong query ${DATA}/pub.world ${DATA}/daily.load ${DATA}/strong.answers
                   --random 800 7 --expect ${DATA}/baseline.answers)
  set_tests_properties(rail_crosscheck_query PROPERTIES FIXTURES_REQUIRED "rail_data;rail_answers")
endif()
