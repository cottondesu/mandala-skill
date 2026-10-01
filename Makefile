.PHONY: build check test

build:
	python3 scripts/build.py

check:
	python3 scripts/validate.py

test:
	python3 -m unittest discover -s tests -p 'test_*.py'
