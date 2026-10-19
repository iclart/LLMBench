PYTHON ?= python
S ?= daily
T ?= 4
.PHONY: all check bench score time submit tsan
all:
	$(PYTHON) bin/workspace.py build
check:
	$(PYTHON) bin/workspace.py check
bench:
	$(PYTHON) bin/workspace.py bench --scenario "$(S)" --threads "$(T)"
score:
	$(PYTHON) bin/workspace.py score
time:
	$(PYTHON) bin/workspace.py time
submit:
	$(PYTHON) bin/workspace.py submit
tsan:
	$(PYTHON) bin/workspace.py tsan
