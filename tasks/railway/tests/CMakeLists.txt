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

# ---- concurrent harness + judge
# Smaller workloads for railbench runs; reference answers from the baseline,
# which itself is cross-checked against the oracle above.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(JUDGE_CASE ${CMAKE_CURRENT_SOURCE_DIR}/judge_case.py)
if(RAIL_TSAN)
  set(SMALL_INTENTS 4000)  # everything runs 5-15x slower under TSan
else()
  set(SMALL_INTENTS 20000)
endif()
foreach(s IN LISTS SCENARIOS)
  add_test(NAME rail_gen_small_${s}
           COMMAND railgen load ${DATA}/pub.world ${CFG}/${s}.cfg 3 ${DATA}/small_${s}.load
                   --set intents=${SMALL_INTENTS} --set quiet_intents=1000)
  set_tests_properties(rail_gen_small_${s} PROPERTIES FIXTURES_REQUIRED rail_world FIXTURES_SETUP rail_small_${s})
  add_test(NAME rail_answers_small_${s}
           COMMAND railrun_baseline query ${DATA}/pub.world ${DATA}/small_${s}.load ${DATA}/small_${s}.answers)
  set_tests_properties(rail_answers_small_${s} PROPERTIES
                       FIXTURES_REQUIRED rail_small_${s} FIXTURES_SETUP rail_small_answers_${s})
endforeach()

# rail_judge_case(<name> <bench target> <scenario> <threads> <expect>)
function(rail_judge_case name bench scenario threads expect)
  add_test(NAME ${name}
           COMMAND ${Python3_EXECUTABLE} ${JUDGE_CASE} --bench $<TARGET_FILE:${bench}> --judge $<TARGET_FILE:railjudge>
                   --world ${DATA}/pub.world --load ${DATA}/small_${scenario}.load
                   --answers ${DATA}/small_${scenario}.answers --threads ${threads}
                   --log ${DATA}/${name}.log --expect ${expect})
  set_tests_properties(${name} PROPERTIES FIXTURES_REQUIRED "rail_small_${scenario};rail_small_answers_${scenario}")
endfunction()

set(BENCH_ENGINES baseline)
if(RAIL_HAVE_STRONG)
  list(APPEND BENCH_ENGINES strong)
endif()
foreach(e IN LISTS BENCH_ENGINES)
  foreach(s IN LISTS SCENARIOS)
    foreach(t 1 4 16)
      rail_judge_case(rail_bench_${e}_${s}_t${t} railbench_${e} ${s} ${t} pass)
    endforeach()
  endforeach()
endforeach()

# ---- mutation tests: every broken engine must be caught.
function(add_rail_mutant name id)
  add_executable(railbench_mutant_${name} ../tools/bench/main.cpp mutants/mutant.cpp)
  target_compile_definitions(railbench_mutant_${name} PRIVATE RAIL_MUTANT=${id})
  target_link_libraries(railbench_mutant_${name} PRIVATE railcommon Threads::Threads)
endfunction()
add_rail_mutant(steal_seats 1)
add_rail_mutant(fake_sold_out 2)
add_rail_mutant(partial_book 3)
add_rail_mutant(data_race 4)
add_rail_mutant(double_refund 5)
add_rail_mutant(wrong_query 6)
add_rail_mutant(bad_seat 7)
add_rail_mutant(busy_sold_out 8)
add_rail_mutant(leaky_refund 9)

rail_judge_case(rail_mutant_steal_seats railbench_mutant_steal_seats spring 4 fail:oversell)
rail_judge_case(rail_mutant_fake_sold_out railbench_mutant_fake_sold_out daily 4 fail:false_negative)
# Leaked seats only surface in the audit, and only where multi-leg books fail (spring).
rail_judge_case(rail_mutant_partial_book railbench_mutant_partial_book spring 4 fail:false_negative)
rail_judge_case(rail_mutant_leaky_refund railbench_mutant_leaky_refund refund_wave 4 fail:false_negative)
rail_judge_case(rail_mutant_double_refund railbench_mutant_double_refund daily 4 fail:refund)
rail_judge_case(rail_mutant_wrong_query railbench_mutant_wrong_query daily 4 fail:query)
rail_judge_case(rail_mutant_bad_seat railbench_mutant_bad_seat daily 4 fail:seat)
rail_judge_case(rail_mutant_busy_sold_out railbench_mutant_busy_sold_out daily 4 fail:false_negative)
# Only wrong under contention: a single thread never sees another call running.
rail_judge_case(rail_mutant_busy_sold_out_t1 railbench_mutant_busy_sold_out daily 1 pass)
if(RAIL_TSAN)
  rail_judge_case(rail_mutant_data_race railbench_mutant_data_race daily 4 bench-fail)
endif()

# ---- evaluation orchestrator (Linux only: needs railexec)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  set(EVAL_SMOKE_ARGS --build ${PROJECT_BINARY_DIR} --work ${DATA}/eval_smoke)
  if(RAIL_HAVE_STRONG)
    list(APPEND EVAL_SMOKE_ARGS --strong)
  endif()
  add_test(NAME rail_eval_smoke COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/eval_smoke.py ${EVAL_SMOKE_ARGS})
  set(REPORT_SMOKE_ARGS --build ${PROJECT_BINARY_DIR} --work ${DATA}/report_smoke)
  if(RAIL_HAVE_STRONG)
    list(APPEND REPORT_SMOKE_ARGS --strong)
  endif()
  add_test(NAME rail_report_smoke COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/report_smoke.py ${REPORT_SMOKE_ARGS})
endif()
