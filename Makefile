.PHONY: build check test release-check

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
