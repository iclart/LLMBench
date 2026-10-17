# Workspace for the LLMBench railway task. See TASK.md, section 10.
#
#   make            build build/railbench and build/railrun from src/
#   make check      the functional suite (data/functional.suite), then all
#                   public scenarios: reference answers, single-threaded
#                   verification, concurrent run judged by railjudge
#   make bench S=spring T=16     one concurrent run and its verdict
#   make tickets S=daily N=40    the tickets your engine issues for the first N
#                   intents (single thread, checked like verify)
#   make score      estimated score on the public workloads, with the official
#                   formula (TASK.md 9); takes a few minutes
#   make tsan       ThreadSanitizer build on small workloads, 4 threads: the
#                   official evaluation scores 0 for any data race
#   make time       time left before the forced submission (check and bench
#                   print it too, as their last line)
#   make submit     submit now (ends the session; src/ can no longer change)

CXX ?= g++
CXXFLAGS := -O2 -std=c++20 -pthread
SRC := $(wildcard src/*.cpp)
HDR := $(wildcard src/*.h src/*.hpp)
SCENARIOS := daily spring refund_wave
S ?= daily
T ?= 4
N ?= 40
WORLD := data/world.world

.PHONY: all check bench tickets score tsan time submit clean FORCE

# Prints the time left before the forced submission, if the runner set one.
TIME_LEFT = if [ -f .deadline ]; then \
	  left=$$(( $$(cat .deadline) - $$(date +%s) )); \
	  if [ $$left -gt 0 ]; then echo "$$(( left / 60 )) min $$(( left % 60 )) s left before the forced submission"; \
	  else echo "time is up: src/ as of the deadline is final"; fi; \
	else echo "no deadline set (not running under the official runner)"; fi

all: build/railbench build/railrun

build:
	@mkdir -p build

# Rebuild whenever the set of source files or their contents change, not
# only when a file looks newer: timestamps miss files moved into src/ and
# edits made within the same clock tick as the last build.
STAMP := build/.sources
$(STAMP): FORCE | build
	@new="$(SRC) $(HDR) $$(cat $(SRC) $(HDR) /dev/null | cksum)"; \
	if [ ! -f $@ ] || [ "$$(cat $@)" != "$$new" ]; then echo "$$new" > $@; fi

build/railbench: $(STAMP) lib/librailbench.a
	$(CXX) $(CXXFLAGS) -Iinclude $(SRC) lib/librailbench.a -o $@

build/railrun: $(STAMP) lib/librailrun.a
	$(CXX) $(CXXFLAGS) -Iinclude $(SRC) lib/librailrun.a -o $@

check: all
	@status=0; echo "== functional suite"; ./build/railrun func $(WORLD) data/functional.suite || status=1; \
	if [ $$status -eq 0 ]; then for s in $(SCENARIOS); do \
	  echo "== $$s: query answers vs reference"; \
	  ./build/railrun query $(WORLD) data/$$s.load build/$$s.answers --expect data/$$s.answers || { status=1; break; }; \
	  echo "== $$s: single-threaded verification"; \
	  ./build/railrun verify $(WORLD) data/$$s.load || { status=1; break; }; \
	  echo "== $$s: $(T) threads, judged"; \
	  ./build/railbench $(WORLD) data/$$s.load $(T) build/$$s.log && \
	  bin/railjudge $(WORLD) data/$$s.load data/$$s.answers build/$$s.log --json build/$$s.json || { status=1; break; }; \
	done; fi; \
	if [ $$status -eq 0 ]; then echo "make check: functional suite and all scenarios passed"; else echo "make check: FAILED"; fi; \
	$(TIME_LEFT); exit $$status

bench: all
	@status=0; \
	./build/railbench $(WORLD) data/$(S).load $(T) build/$(S)_t$(T).log && \
	bin/railjudge $(WORLD) data/$(S).load data/$(S).answers build/$(S)_t$(T).log --json build/$(S)_t$(T).json && \
	cat build/$(S)_t$(T).json || status=1; \
	$(TIME_LEFT); exit $$status

tickets: all
	./build/railrun tickets $(WORLD) data/$(S).load --limit $(N)

score: all
	@status=0; python3 bin/railscore.py || status=1; $(TIME_LEFT); exit $$status

build/railbench_tsan: $(STAMP) lib/librailbench_tsan.a
	$(CXX) $(CXXFLAGS) -fsanitize=thread -g -Iinclude $(SRC) lib/librailbench_tsan.a -o $@

tsan: build/railbench_tsan
	@status=0; for s in $(SCENARIOS); do \
	  echo "== $$s: ThreadSanitizer, 4 threads"; \
	  TSAN_OPTIONS="halt_on_error=1 exitcode=66" ./build/railbench_tsan $(WORLD) data/tsan_$$s.load 4 build/tsan_$$s.log && \
	  bin/railjudge $(WORLD) data/tsan_$$s.load data/tsan_$$s.answers build/tsan_$$s.log --json build/tsan_$$s.json \
	    || { status=1; break; }; \
	done; \
	if [ $$status -eq 0 ]; then echo "make tsan: no data race found"; else echo "make tsan: FAILED"; fi; \
	$(TIME_LEFT); exit $$status

time:
	@$(TIME_LEFT)

submit:
	@date +%s > .submit
	@echo "Submitted. The session ends now; src/ is final."

clean:
	rm -rf build
