.PHONY: build check test release-check eval-live

build:
	python3 scripts/build.py

check:
	python3 scripts/validate.py

test:
	python3 -m unittest discover -s tests -p 'test_*.py'

release-check:
	$(MAKE) build
	$(MAKE) check
	$(MAKE) test
	python3 scripts/release_check.py

# Live agents run only when invoked explicitly; never part of check, test, release-check, or CI.
AGENT ?= codex
SUITE ?= release

# Values reach Python through exported environment variables, never through shell parsing.
eval-live: export EVAL_LIVE_AGENT = $(AGENT)
eval-live: export EVAL_LIVE_SUITE = $(SUITE)
eval-live:
	$(MAKE) build
	python3 scripts/eval_live.py --agent "$$EVAL_LIVE_AGENT" --suite "$$EVAL_LIVE_SUITE"
