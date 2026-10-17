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

foreach(s IN LISTS SCENARIOS)
  add_test(NAME rail_verify_baseline_${s}
           COMMAND railrun_baseline verify ${DATA}/pub.world ${DATA}/${s}.load --limit 5000)
  set_tests_properties(rail_verify_baseline_${s} PROPERTIES FIXTURES_REQUIRED rail_data)
endforeach()
# The oracle is exhaustive and slow, so it only replays a short prefix.
add_test(NAME rail_verify_oracle COMMAND railrun_oracle verify ${DATA}/pub.world ${DATA}/spring.load --limit 300)
set_tests_properties(rail_verify_oracle PROPERTIES FIXTURES_REQUIRED rail_data)

# query() answers of every engine must match the oracle's byte for byte.
add_test(NAME rail_answers_oracle
         COMMAND railrun_oracle query ${DATA}/pub.world ${DATA}/daily.load ${DATA}/oracle.answers --random 800 7)
set_tests_properties(rail_answers_oracle PROPERTIES FIXTURES_REQUIRED rail_data FIXTURES_SETUP rail_answers)
set(CROSSCHECK_ENGINES baseline)

if(RAIL_HAVE_STRONG)
  foreach(s IN LISTS SCENARIOS)
    add_test(NAME rail_verify_strong_${s} COMMAND railrun_strong verify ${DATA}/pub.world ${DATA}/${s}.load)
    set_tests_properties(rail_verify_strong_${s} PROPERTIES FIXTURES_REQUIRED rail_data)
  endforeach()
  list(APPEND CROSSCHECK_ENGINES strong)
endif()

foreach(e IN LISTS CROSSCHECK_ENGINES)
  add_test(NAME rail_crosscheck_${e}
           COMMAND railrun_${e} query ${DATA}/pub.world ${DATA}/daily.load ${DATA}/${e}.answers
                   --random 800 7 --expect ${DATA}/oracle.answers)
  set_tests_properties(rail_crosscheck_${e} PROPERTIES FIXTURES_REQUIRED "rail_data;rail_answers")
endforeach()
