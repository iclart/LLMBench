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
find_package(Python3 COMPONENTS Interpreter REQUIRED)
add_test(NAME rail_coarse_clock COMMAND ${Python3_EXECUTABLE}
         ${CMAKE_CURRENT_SOURCE_DIR}/coarse_clock.py --judge $<TARGET_FILE:railjudge>
         --work ${CMAKE_CURRENT_BINARY_DIR}/coarse_clock)

# ---- engine checks on generated public data
set(CFG ${CMAKE_CURRENT_SOURCE_DIR}/../configs/public)
set(DATA ${CMAKE_CURRENT_BINARY_DIR}/data)
file(MAKE_DIRECTORY ${DATA})
set(SCENARIOS daily spring refund_wave)

# Sanitizer builds run everything 5-15x slower: they get smaller workloads.
if(RAIL_TSAN OR CMAKE_CXX_FLAGS MATCHES "-fsanitize")
  set(RAIL_SLOW_BUILD ON)
  set(VERIFY_LIMIT 1500)
  set(ORACLE_VERIFY_LIMIT 100)
  set(CROSSCHECK_QUERIES 200)
else()
  set(RAIL_SLOW_BUILD OFF)
  set(VERIFY_LIMIT 5000)
  set(ORACLE_VERIFY_LIMIT 300)
  set(CROSSCHECK_QUERIES 800)
endif()

add_test(NAME rail_gen_world COMMAND railgen world ${CFG}/world.cfg 1 ${DATA}/pub.world)
set_tests_properties(rail_gen_world PROPERTIES FIXTURES_SETUP rail_world)
foreach(s IN LISTS SCENARIOS)
  add_test(NAME rail_gen_${s} COMMAND railgen load ${DATA}/pub.world ${CFG}/${s}.cfg 2 ${DATA}/${s}.load)
  set_tests_properties(rail_gen_${s} PROPERTIES FIXTURES_REQUIRED rail_world FIXTURES_SETUP rail_data)
endforeach()

foreach(s IN LISTS SCENARIOS)
  add_test(NAME rail_verify_baseline_${s}
           COMMAND railrun_baseline verify ${DATA}/pub.world ${DATA}/${s}.load --limit ${VERIFY_LIMIT})
  set_tests_properties(rail_verify_baseline_${s} PROPERTIES FIXTURES_REQUIRED rail_data)
endforeach()
add_test(NAME rail_tickets_baseline COMMAND railrun_baseline tickets ${DATA}/pub.world ${DATA}/spring.load --limit 300)
set_tests_properties(rail_tickets_baseline PROPERTIES FIXTURES_REQUIRED rail_data)
# The oracle is exhaustive and slow, so it only replays a short prefix.
add_test(NAME rail_verify_oracle COMMAND railrun_oracle verify ${DATA}/pub.world ${DATA}/spring.load --limit ${ORACLE_VERIFY_LIMIT})
set_tests_properties(rail_verify_oracle PROPERTIES FIXTURES_REQUIRED rail_data)

# query() answers of every engine must match the oracle's byte for byte.
add_test(NAME rail_answers_oracle
         COMMAND railrun_oracle query ${DATA}/pub.world ${DATA}/daily.load ${DATA}/oracle.answers --random ${CROSSCHECK_QUERIES} 7)
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
                   --random ${CROSSCHECK_QUERIES} 7 --expect ${DATA}/oracle.answers)
  set_tests_properties(rail_crosscheck_${e} PROPERTIES FIXTURES_REQUIRED "rail_data;rail_answers")
endforeach()

# ---- functional suite (eval/make_suite.py, railrun func): generated from the
# baseline's answers, passed by the independent oracle, failed by the stub.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
add_test(NAME rail_func_suite
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/../eval/make_suite.py --world ${DATA}/pub.world
                 --reference $<TARGET_FILE:railrun_baseline> --seed 9 --out ${DATA}/func.suite --per-category 8)
set_tests_properties(rail_func_suite PROPERTIES FIXTURES_REQUIRED rail_world FIXTURES_SETUP rail_func)
add_test(NAME rail_func_oracle COMMAND railrun_oracle func ${DATA}/pub.world ${DATA}/func.suite)
set_tests_properties(rail_func_oracle PROPERTIES FIXTURES_REQUIRED rail_func)
add_test(NAME rail_func_stub COMMAND railrun_stub func ${DATA}/pub.world ${DATA}/func.suite)
set_tests_properties(rail_func_stub PROPERTIES FIXTURES_REQUIRED rail_func WILL_FAIL TRUE)

# ---- concurrent harness + judge
# Smaller workloads for railbench runs; reference answers from the baseline,
# which itself is cross-checked against the oracle above.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(JUDGE_CASE ${CMAKE_CURRENT_SOURCE_DIR}/judge_case.py)
if(RAIL_SLOW_BUILD)
  set(SMALL_INTENTS 4000)
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
add_rail_mutant(withhold_long 10)
# The withholding engine is legal; railrun_mutant_withhold_long lets
# experiments replay it single-threaded (railrun verify prints its quality).
add_executable(railrun_mutant_withhold_long ../tools/run/main.cpp mutants/mutant.cpp)
target_compile_definitions(railrun_mutant_withhold_long PRIVATE RAIL_MUTANT=10)
target_link_libraries(railrun_mutant_withhold_long PRIVATE railcommon Threads::Threads)

rail_judge_case(rail_mutant_steal_seats railbench_mutant_steal_seats spring 4 fail:oversell)
rail_judge_case(rail_mutant_fake_sold_out railbench_mutant_fake_sold_out daily 4 fail:false_negative)
# Leaked seats only surface in the audit, and only where multi-leg books fail (spring).
rail_judge_case(rail_mutant_partial_book railbench_mutant_partial_book spring 4 fail:false_negative)
rail_judge_case(rail_mutant_leaky_refund railbench_mutant_leaky_refund refund_wave 4 fail:false_negative)
rail_judge_case(rail_mutant_double_refund railbench_mutant_double_refund daily 4 fail:refund)
rail_judge_case(rail_mutant_wrong_query railbench_mutant_wrong_query daily 4 fail:query)
rail_judge_case(rail_mutant_bad_seat railbench_mutant_bad_seat daily 4 fail:seat)
rail_judge_case(rail_mutant_busy_sold_out railbench_mutant_busy_sold_out daily 4 fail:false_negative)
# Withholding is a sales policy, not an error.
rail_judge_case(rail_mutant_withhold_long railbench_mutant_withhold_long spring 4 pass)
# Only wrong under contention: a single thread never sees another call running.
rail_judge_case(rail_mutant_busy_sold_out_t1 railbench_mutant_busy_sold_out daily 1 pass)
if(RAIL_TSAN)
  rail_judge_case(rail_mutant_data_race railbench_mutant_data_race daily 4 bench-fail)
endif()

# ---- evaluation orchestrator (Linux only: needs railexec)
add_test(NAME rail_opencode_credentials COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/opencode_credentials.py)
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
  # The packaged workspace: whitelist, stub builds, baseline passes make check/tsan/score.
  if(RAIL_HAVE_STRONG)
    set(PACKAGE_ANSWERS strong)
    set(PACKAGE_ANCHORS measure)
  else()
    set(PACKAGE_ANSWERS baseline)
    set(PACKAGE_ANCHORS none)
  endif()
  add_test(NAME rail_package_selftest COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/tasks/railway/agent/package_workspace.py
           --build ${PROJECT_BINARY_DIR} --out ${DATA}/package_selftest/workspace --answers-engine ${PACKAGE_ANSWERS}
           --anchors ${PACKAGE_ANCHORS} --intents 3000 --selftest)
endif()
