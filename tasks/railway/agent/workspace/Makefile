# Workspace for the LLMBench railway task. See TASK.md, section 10.
#
#   make            build build/railbench and build/railrun from src/
#   make check      all public scenarios: reference answers, single-threaded
#                   verification, concurrent run judged by railjudge
#   make bench S=spring T=16     one concurrent run and its verdict
#   make time       time left before the forced submission
#   make submit     submit now (ends the session; src/ can no longer change)

CXX ?= g++
CXXFLAGS := -O2 -std=c++20 -pthread
SRC := $(wildcard src/*.cpp)
HDR := $(wildcard src/*.h src/*.hpp)
SCENARIOS := daily spring refund_wave
S ?= daily
T ?= 4
WORLD := data/world.world

.PHONY: all check bench time submit clean

all: build/railbench build/railrun

build:
	@mkdir -p build

build/railbench: $(SRC) $(HDR) lib/librailbench.a | build
	$(CXX) $(CXXFLAGS) -Iinclude $(SRC) lib/librailbench.a -o $@

build/railrun: $(SRC) $(HDR) lib/librailrun.a | build
	$(CXX) $(CXXFLAGS) -Iinclude $(SRC) lib/librailrun.a -o $@

check: all
	@for s in $(SCENARIOS); do \
	  echo "== $$s: query answers vs reference"; \
	  ./build/railrun query $(WORLD) data/$$s.load build/$$s.answers --expect data/$$s.answers || exit 1; \
	  echo "== $$s: single-threaded verification"; \
	  ./build/railrun verify $(WORLD) data/$$s.load || exit 1; \
	  echo "== $$s: $(T) threads, judged"; \
	  ./build/railbench $(WORLD) data/$$s.load $(T) build/$$s.log && \
	  bin/railjudge $(WORLD) data/$$s.load data/$$s.answers build/$$s.log --json build/$$s.json || exit 1; \
	done
	@echo "make check: all scenarios passed"

bench: all
	./build/railbench $(WORLD) data/$(S).load $(T) build/$(S)_t$(T).log
	bin/railjudge $(WORLD) data/$(S).load data/$(S).answers build/$(S)_t$(T).log --json build/$(S)_t$(T).json
	@cat build/$(S)_t$(T).json

time:
	@if [ -f .deadline ]; then \
	  left=$$(( $$(cat .deadline) - $$(date +%s) )); \
	  echo "$$(( left / 60 )) min $$(( left % 60 )) s left before the forced submission"; \
	else echo "no deadline set (not running under the official runner)"; fi

submit:
	@date +%s > .submit
	@echo "Submitted. The session ends now; src/ is final."

clean:
	rm -rf build
